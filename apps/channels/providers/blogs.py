"""Long-form destinations: WordPress (REST API) and Drupal (JSON:API).

Post bodies are written in Markdown in Markette and rendered to HTML here.
"""

import base64
import html as htmllib
import re
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

import markdown as md
from django.conf import settings
from django.utils.text import slugify

from .base import (
    BaseProvider,
    Candidate,
    Capabilities,
    CredentialField,
    HistoryEntry,
    InboxEntry,
    Metrics,
    ProviderError,
    PublishResult,
    repeatable,
)
from .text import html_to_text, parse_time

BLOG_CAPS = dict(
    kind="blog",
    supports_edit=True,
    max_chars=None,
    max_media=20,
    supports_title=True,
    auth="credentials",
    instance_url=True,
    metric_names=("comments",),
)


def render_html(text):
    return md.markdown(text or "", extensions=["extra", "sane_lists", "smarty"])


def normalize_site(url):
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ProviderError("Site URL is required.")
    if "://" not in url:
        url = f"https://{url}"
    if not urlparse(url).netloc:
        raise ProviderError("Invalid site URL.")
    return url


def plain_text(html):
    """The words of an HTML body, to compare what a site stored with what was sent (images and markup aside)."""
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def basic_auth(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def inline_images(html, media, mapping):
    """Swap Markette media URLs in the body for the URLs the site assigned on upload."""
    for item in media:
        if item.url in mapping:
            html = html.replace(item.url, mapping[item.url])
    return html


class WordPressProvider(BaseProvider):
    key = "wordpress"
    label = "WordPress"
    duplicate_guard = "lookup"
    capabilities = Capabilities(
        **{**BLOG_CAPS, "inbox": True, "inbox_interval": 10, "inbox_reply": True, "history": True}
    )
    credential_fields = [
        CredentialField("base_url", "Site URL", type="url", help="e.g. https://blog.example.com"),
        CredentialField("username", "Username"),
        CredentialField(
            "app_password",
            "Application password",
            type="password",
            help="Users → Profile → Application Passwords in wp-admin.",
        ),
        CredentialField(
            "default_status",
            "Publish as",
            type="select",
            required=False,
            default="publish",
            choices=["publish", "draft", "pending", "private"],
        ),
    ]

    def connect_with_credentials(self, data):
        site = normalize_site(data.get("base_url"))
        auth = basic_auth(data["username"].strip(), data["app_password"].replace(" ", ""))
        me = self.http(
            "GET", f"{site}/wp-json/wp/v2/users/me", params={"context": "edit"}, headers={"Authorization": auth}
        ).json()
        caps = me.get("capabilities") or {}
        if caps and not (caps.get("publish_posts") or caps.get("edit_posts")):
            raise ProviderError("This WordPress user cannot publish posts.")
        info = self.http("GET", f"{site}/wp-json").json()
        return [
            Candidate(
                external_id=f"{site}#{me['id']}",
                name=info.get("name") or urlparse(site).netloc,
                handle=me.get("name") or data["username"],
                avatar_url=(me.get("avatar_urls") or {}).get("96", ""),
                profile_url=site,
                base_url=site,
                credentials={
                    "username": data["username"].strip(),
                    "app_password": data["app_password"].replace(" ", ""),
                },
                config={"default_status": data.get("default_status") or "publish"},
            )
        ]

    @property
    def api(self):
        return f"{self.channel.base_url}/wp-json/wp/v2"

    @property
    def headers(self):
        return {"Authorization": basic_auth(self.creds["username"], self.creds["app_password"])}

    @repeatable
    def _upload(self, item):
        res = self.http(
            "POST",
            f"{self.api}/media",
            content=item.read(),
            headers={
                **self.headers,
                "Content-Type": item.mime,
                "Content-Disposition": f'attachment; filename="{item.filename or "upload"}"',
            },
        ).json()
        if item.alt:
            self.http("POST", f"{self.api}/media/{res['id']}", headers=self.headers, json={"alt_text": item.alt})
        return res

    @repeatable
    def _terms(self, taxonomy, names):
        ids = []
        for name in names:
            found = self.http(
                "GET", f"{self.api}/{taxonomy}", headers=self.headers, params={"search": name, "per_page": 20}
            ).json()
            match = next((t for t in found if t["name"].lower() == name.lower()), None)
            if match is None:
                match = self.http("POST", f"{self.api}/{taxonomy}", headers=self.headers, json={"name": name}).json()
            ids.append(match["id"])
        return ids

    def publish(self, content):
        uploaded = {m.url: self._upload(m) for m in content.media}
        html = inline_images(
            render_html(content.text), content.media, {u: r.get("source_url", u) for u, r in uploaded.items()}
        )
        opts = content.options
        body = {
            "title": content.title,
            "content": html,
            "status": opts.get("status") or self.channel.config.get("default_status", "publish"),
        }
        if opts.get("excerpt"):
            body["excerpt"] = opts["excerpt"]
        if opts.get("slug"):
            body["slug"] = opts["slug"]
        if content.media:
            featured = opts.get("featured_media_index", 0)
            body["featured_media"] = list(uploaded.values())[min(featured, len(uploaded) - 1)]["id"]
        if opts.get("categories"):
            body["categories"] = self._terms("categories", opts["categories"])
        tags = opts.get("tags") or content.tags
        if tags:
            body["tags"] = self._terms("tags", tags)
        post = self.http("POST", f"{self.api}/posts", headers=self.headers, json=body).json()
        media_map = {u: r.get("source_url", u) for u, r in uploaded.items()}
        return PublishResult(external_id=str(post["id"]), url=post.get("link", ""), extra={"media_map": media_map})

    def fetch_history(self, limit=100):
        out, page = [], 1
        while len(out) < limit:
            try:
                posts = self.http(
                    "GET",
                    f"{self.api}/posts",
                    headers=self.headers,
                    params={
                        "status": "publish",
                        "per_page": 100,
                        "page": page,
                        "_fields": "id,date_gmt,link,title,content",
                    },
                ).json()
            except ProviderError as exc:
                if exc.status == 400:  # past the last page
                    break
                raise
            for p in posts:
                out.append(
                    HistoryEntry(
                        external_id=str(p["id"]),
                        published_at=parse_time(p.get("date_gmt")),
                        title=html_to_text((p.get("title") or {}).get("rendered", "")),
                        text=html_to_text((p.get("content") or {}).get("rendered", ""))[:5000],
                        url=p.get("link", ""),
                    )
                )
            if len(posts) < 100:
                break
            page += 1
        return out[:limit]

    def fetch_inbox(self, state, recent_posts):
        if not recent_posts:
            return [], state
        comments = self.http(
            "GET",
            f"{self.api}/comments",
            headers=self.headers,
            params={"post": ",".join(recent_posts[:20]), "per_page": 50, "orderby": "date", "order": "desc"},
        ).json()
        me = (self.creds.get("username") or "").lower()
        entries = []
        for c in comments:
            if (c.get("author_name") or "").lower() == me:
                continue
            entries.append(
                InboxEntry(
                    external_id=str(c["id"]),
                    kind="comment",
                    text=html_to_text((c.get("content") or {}).get("rendered", "")),
                    url=c.get("link", ""),
                    author_name=c.get("author_name", ""),
                    author_avatar=(c.get("author_avatar_urls") or {}).get("96", ""),
                    author_url=c.get("author_url", ""),
                    created_at=parse_time(c.get("date_gmt")),
                    post_external_id=str(c.get("post", "")),
                    context={"post": c.get("post")},
                )
            )
        return entries, state

    def reply_inbox(self, entry, text):
        c = self.http(
            "POST",
            f"{self.api}/comments",
            headers=self.headers,
            json={"post": entry.context.get("post"), "parent": int(entry.external_id), "content": text},
        ).json()
        return PublishResult(external_id=str(c["id"]), url=c.get("link", ""))

    def update(self, external_id, content, extra):
        html = inline_images(render_html(content.text), content.media, extra.get("media_map") or {})
        body = {"title": content.title, "content": html}
        if content.options.get("excerpt"):
            body["excerpt"] = content.options["excerpt"]
        post = self.http("POST", f"{self.api}/posts/{external_id}", headers=self.headers, json=body).json()
        return {"url": post.get("link")} if post.get("link") else None

    def find_published(self, content, since):
        # Your latest posts in any state, as stored (context=edit gives the raw title and body).
        posts = self.http(
            "GET",
            f"{self.api}/posts",
            headers=self.headers,
            params={
                "context": "edit",
                "status": "publish,future,draft,pending,private",
                "orderby": "date",
                "per_page": 20,
            },
        ).json()
        words = plain_text(render_html(content.text))
        for p in posts:
            touched = datetime.fromisoformat(p.get("modified_gmt") or "1970-01-01T00:00:00").replace(tzinfo=UTC)
            raw = (p.get("title") or {}).get("raw", "")
            if touched >= since and raw == content.title and plain_text((p.get("content") or {}).get("raw")) == words:
                return PublishResult(external_id=str(p["id"]), url=p.get("link", ""))
        return None

    def delete(self, external_id):
        self.http("DELETE", f"{self.api}/posts/{external_id}", headers=self.headers)

    def fetch_metrics(self, external_ids):
        out = {}
        for pid in external_ids:
            resp = self.http(
                "GET",
                f"{self.api}/comments",
                headers=self.headers,
                params={"post": pid, "per_page": 1, "status": "approve"},
            )
            out[pid] = Metrics(comments=int(resp.headers.get("X-WP-Total", 0)))
        return out


class DrupalProvider(BaseProvider):
    key = "drupal"
    label = "Drupal"
    # Each post gets a UUID derived from its publication, so the earlier attempt can be read back.
    duplicate_guard = "lookup"
    capabilities = Capabilities(**BLOG_CAPS)
    credential_fields = [
        CredentialField("base_url", "Site URL", type="url", help="Drupal 9+ with the JSON:API module enabled."),
        CredentialField(
            "auth_method",
            "Authentication",
            type="select",
            default="basic",
            choices=["basic", "bearer"],
            help="basic needs the HTTP Basic Auth module; bearer uses a Simple OAuth token.",
        ),
        CredentialField("username", "Username", required=False),
        CredentialField("password", "Password or token", type="password"),
        CredentialField("bundle", "Content type", required=False, default="article"),
        CredentialField("body_format", "Text format", required=False, default="basic_html"),
        CredentialField("image_field", "Image field", required=False, default="field_image"),
        CredentialField("tags_field", "Tags field", required=False, default="field_tags"),
    ]

    def connect_with_credentials(self, data):
        site = normalize_site(data.get("base_url"))
        creds = {
            "auth_method": data.get("auth_method") or "basic",
            "username": (data.get("username") or "").strip(),
            "password": data["password"],
        }
        config = {
            "bundle": data.get("bundle") or "article",
            "body_format": data.get("body_format") or "basic_html",
            "image_field": data.get("image_field") or "field_image",
            "tags_field": data.get("tags_field") or "field_tags",
        }
        index = self.http(
            "GET", f"{site}/jsonapi", headers={**self._auth(creds), "Accept": "application/vnd.api+json"}
        ).json()
        resource = f"node--{config['bundle']}"
        if resource not in (index.get("links") or {}):
            raise ProviderError(
                f"JSON:API resource {resource} not found. Is the content type correct and JSON:API enabled?"
            )
        me = (index.get("meta") or {}).get("links", {}).get("me", {}).get("meta", {}).get("id", "")
        return [
            Candidate(
                external_id=f"{site}#{config['bundle']}",
                name=urlparse(site).netloc,
                handle=creds["username"] or me,
                profile_url=site,
                base_url=site,
                credentials=creds,
                config=config,
            )
        ]

    @staticmethod
    def _auth(creds):
        if creds.get("auth_method") == "bearer":
            return {"Authorization": f"Bearer {creds['password']}"}
        return {"Authorization": basic_auth(creds["username"], creds["password"])}

    @property
    def headers(self):
        return {**self._auth(self.creds), "Accept": "application/vnd.api+json"}

    @property
    def node_url(self):
        return f"{self.channel.base_url}/jsonapi/node/{self.channel.config.get('bundle', 'article')}"

    @repeatable
    def _upload(self, item):
        field = self.channel.config.get("image_field", "field_image")
        res = self.http(
            "POST",
            f"{self.node_url}/{field}",
            content=item.read(),
            headers={
                **self.headers,
                "Content-Type": "application/octet-stream",
                "Content-Disposition": f'file; filename="{item.filename or "upload"}"',
            },
        ).json()["data"]
        return res

    @repeatable
    def _tags(self, names):
        refs = []
        url = f"{self.channel.base_url}/jsonapi/taxonomy_term/tags"
        for name in names:
            found = self.http("GET", url, headers=self.headers, params={"filter[name]": name}).json()["data"]
            if not found:
                found = [
                    self.http(
                        "POST",
                        url,
                        headers={**self.headers, "Content-Type": "application/vnd.api+json"},
                        json={"data": {"type": "taxonomy_term--tags", "attributes": {"name": name}}},
                    ).json()["data"]
                ]
            refs.append({"type": "taxonomy_term--tags", "id": found[0]["id"]})
        return refs

    def publish(self, content):
        cfg = self.channel.config
        opts = content.options
        files = [(m, self._upload(m)) for m in content.media if not m.is_video]
        mapping = {}
        for m, f in files:
            uri = ((f.get("attributes") or {}).get("uri") or {}).get("url")
            if uri:
                mapping[m.url] = uri if uri.startswith("http") else f"{self.channel.base_url}{uri}"
        html = inline_images(render_html(content.text), content.media, mapping)
        self._media_map = mapping
        attributes = {
            "title": content.title,
            "body": {"value": html, "format": cfg.get("body_format", "basic_html")},
            "status": opts.get("status", "publish") != "draft",
        }
        if opts.get("excerpt"):
            attributes["body"]["summary"] = opts["excerpt"]
        if opts.get("slug"):
            attributes["path"] = {"alias": "/" + slugify(opts["slug"])}
        relationships = {}
        if files:
            m, f = files[0]
            relationships[cfg.get("image_field", "field_image")] = {
                "data": {"type": "file--file", "id": f["id"], "meta": {"alt": m.alt or content.title}},
            }
        tags = opts.get("tags") or content.tags
        if tags and cfg.get("tags_field"):
            relationships[cfg["tags_field"]] = {"data": self._tags(tags)}
        body = {
            "data": {
                "type": f"node--{cfg.get('bundle', 'article')}",
                "id": self.node_uuid(content),
                "attributes": attributes,
            }
        }
        if relationships:
            body["data"]["relationships"] = relationships
        node = self.http(
            "POST", self.node_url, json=body, headers={**self.headers, "Content-Type": "application/vnd.api+json"}
        ).json()["data"]
        result = self._result(node)
        result.extra["media_map"] = self._media_map
        return result

    def update(self, external_id, content, extra):
        cfg = self.channel.config
        html = inline_images(render_html(content.text), content.media, extra.get("media_map") or {})
        body = {"value": html, "format": cfg.get("body_format", "basic_html")}
        if content.options.get("excerpt"):
            body["summary"] = content.options["excerpt"]
        self.http(
            "PATCH",
            f"{self.node_url}/{external_id}",
            headers={**self.headers, "Content-Type": "application/vnd.api+json"},
            json={
                "data": {
                    "type": f"node--{cfg.get('bundle', 'article')}",
                    "id": external_id,
                    "attributes": {"title": content.title, "body": body},
                }
            },
        )

    def node_uuid(self, content):
        """Stable per publication and per Markette server (JSON:API accepts client-generated IDs)."""
        return str(uuid5(NAMESPACE_URL, f"{settings.PUBLIC_API_URL}/{content.idempotency_key}"))

    def _result(self, node):
        attrs = node.get("attributes", {})
        alias = (attrs.get("path") or {}).get("alias")
        nid = attrs.get("drupal_internal__nid")
        url = f"{self.channel.base_url}{alias}" if alias else f"{self.channel.base_url}/node/{nid}"
        return PublishResult(external_id=node["id"], url=url, extra={"nid": nid})

    def find_published(self, content, since):
        try:
            node = self.http("GET", f"{self.node_url}/{self.node_uuid(content)}", headers=self.headers).json()["data"]
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        return self._result(node)

    def delete(self, external_id):
        self.http("DELETE", f"{self.node_url}/{external_id}", headers=self.headers)

    def fetch_metrics(self, external_ids):
        out = {}
        for uuid in external_ids:
            try:
                node = self.http("GET", f"{self.node_url}/{uuid}", headers=self.headers).json()["data"]
            except ProviderError as exc:
                if exc.status == 404:
                    continue
                raise
            comment = (node.get("attributes") or {}).get("comment") or {}
            out[uuid] = Metrics(comments=comment.get("comment_count"), raw={"comment": comment})
        return out

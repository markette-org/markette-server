"""dev.to (and other Forem communities) through the Forem API v1 and a personal API key.

Articles are Markdown with a title, up to four tags, an optional cover image, a canonical URL
and a series. Forem has no image upload API: images are referenced by their public URL, so
the server's /media/ path must be reachable from the internet. The API cannot delete
articles; "delete" unpublishes them (they go back to drafts).
"""

import re

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
)
from .text import html_to_text, parse_time

DEFAULT_BASE = "https://dev.to"
ACCEPT = "application/vnd.forem.api-v1+json"
MAX_TAGS = 4
TITLE_MAX = 128


def normalize_tags(tags):
    """Forem tags are lowercase letters and digits only."""
    out = []
    for tag in tags:
        t = re.sub(r"[^a-z0-9]", "", str(tag).lower())
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


class DevtoProvider(BaseProvider):
    key = "devto"
    label = "DEV"
    duplicate_guard = "lookup"
    capabilities = Capabilities(
        kind="blog",
        max_chars=None,
        max_media=20,
        supports_video=False,
        supports_title=True,
        supports_edit=True,
        history=True,
        # Forem's API lists comments but cannot post them: the inbox links to the article to answer.
        inbox=True,
        inbox_interval=15,
        metric_names=("impressions", "likes", "comments"),
        auth="credentials",
    )
    credential_fields = [
        CredentialField(
            "api_key",
            "API key",
            type="password",
            help="dev.to → Settings → Extensions → DEV Community API Keys → Generate API Key.",
        ),
        CredentialField(
            "organization",
            "Organization",
            required=False,
            help="Optional: the organization's username (from dev.to/<username>) to publish under it. You must "
            "be a member of it.",
        ),
        CredentialField(
            "default_status",
            "Publish as",
            type="select",
            required=False,
            default="publish",
            choices=["publish", "draft"],
        ),
        CredentialField(
            "base_url",
            "Community",
            type="url",
            required=False,
            help="Leave empty for dev.to. For another Forem community, its address, e.g. https://community.example.org.",
        ),
    ]

    # --- API ------------------------------------------------------------------------------
    @property
    def base(self):
        return (self.channel.base_url if self.channel else "") or DEFAULT_BASE

    def _headers(self, key=None):
        return {"api-key": key or self.creds["api_key"], "Accept": ACCEPT}

    def error_from_response(self, resp):
        err = super().error_from_response(resp)
        if resp.status_code == 403:
            err.auth = True  # Forem answers 403 for a wrong or revoked key on some endpoints.
        return err

    # --- connection -------------------------------------------------------------------------
    def connect_with_credentials(self, data):
        base = (data.get("base_url") or "").strip().rstrip("/") or DEFAULT_BASE
        if not re.match(r"^https?://[^/\s]+$", base):
            raise ProviderError("Enter the community's address, e.g. https://dev.to.")
        key = data["api_key"].strip()
        try:
            me = self.http("GET", f"{base}/api/users/me", headers=self._headers(key)).json()
        except ProviderError as exc:
            if exc.status in (401, 403):
                raise ProviderError("The community rejected this API key.") from exc
            raise
        config = {"default_status": data.get("default_status") or "publish", "username": me["username"]}
        org_name = (data.get("organization") or "").strip().lstrip("@").strip("/").rsplit("/", 1)[-1]
        if org_name:
            try:
                org = self.http("GET", f"{base}/api/organizations/{org_name}", headers=self._headers(key)).json()
            except ProviderError as exc:
                raise ProviderError(f"No organization named {org_name} on {base}.") from exc
            return [
                Candidate(
                    external_id=f"org:{org['id']}",
                    name=org.get("name") or org_name,
                    handle=f"@{org.get('username', org_name)} · via @{me['username']}",
                    avatar_url=org.get("profile_image", ""),
                    profile_url=f"{base}/{org.get('username', org_name)}",
                    base_url=base if base != DEFAULT_BASE else "",
                    credentials={"api_key": key},
                    config={**config, "organization_id": org["id"]},
                )
            ]
        return [
            Candidate(
                external_id=f"user:{me['id']}",
                name=me.get("name") or me["username"],
                handle=f"@{me['username']}",
                avatar_url=me.get("profile_image", ""),
                profile_url=f"{base}/{me['username']}",
                base_url=base if base != DEFAULT_BASE else "",
                credentials={"api_key": key},
                config=config,
            )
        ]

    # --- publishing -------------------------------------------------------------------------
    def validate(self, content):
        problems = super().validate(content)
        if len(content.title) > TITLE_MAX:
            problems.append(f"The title exceeds {TITLE_MAX} characters.")
        return problems

    def _article(self, content):
        cfg = self.channel.config
        opts = content.options
        body = content.text
        images = [m for m in content.media if not m.is_video]
        cover = images[0] if images else None
        for item in images[1:]:
            if item.url not in body:
                # Images attached but not referenced in the text go after it.
                body += f"\n\n![{item.alt}]({item.url})"
        status = opts.get("status") or cfg.get("default_status", "publish")
        article = {
            "title": content.title[:TITLE_MAX],
            "body_markdown": body,
            "published": status != "draft",
            "tags": normalize_tags(opts.get("tags") or content.tags),
        }
        if cover:
            article["main_image"] = cover.url
        canonical = opts.get("canonical_url") or content.link
        if canonical:
            article["canonical_url"] = canonical
        if opts.get("excerpt"):
            article["description"] = opts["excerpt"][:300]
        if opts.get("series"):
            article["series"] = opts["series"][:100]
        if cfg.get("organization_id"):
            article["organization_id"] = cfg["organization_id"]
        return article

    def publish(self, content):
        resp = self.http(
            "POST", f"{self.base}/api/articles", headers=self._headers(), json={"article": self._article(content)}
        )
        data = resp.json()
        return PublishResult(external_id=str(data["id"]), url=data.get("url", ""))

    def update(self, external_id, content, extra):
        article = self._article(content)
        article.pop("organization_id", None)
        article.pop("published", None)  # keep it published (or a draft) as it is
        self.http("PUT", f"{self.base}/api/articles/{external_id}", headers=self._headers(), json={"article": article})

    def fetch_history(self, limit=100):
        out, page = [], 1
        while len(out) < limit:
            items = self.http(
                "GET",
                f"{self.base}/api/articles/me/published",
                headers=self._headers(),
                params={"page": page, "per_page": 100},
            ).json()
            for a in items:
                out.append(
                    HistoryEntry(
                        external_id=str(a["id"]),
                        published_at=parse_time(a.get("published_at")),
                        title=a.get("title", ""),
                        text=a.get("description", ""),
                        url=a.get("url", ""),
                        metrics=Metrics(
                            impressions=a.get("page_views_count"),
                            likes=a.get("public_reactions_count"),
                            comments=a.get("comments_count"),
                        ),
                    )
                )
            if len(items) < 100:
                break
            page += 1
        return out[:limit]

    def fetch_inbox(self, state, recent_posts):
        me = (self.channel.config.get("username") or "").lower()
        entries = []
        for aid in recent_posts[:10]:
            tree = self.http("GET", f"{self.base}/api/comments", headers=self._headers(), params={"a_id": aid}).json()
            stack = list(tree)
            while stack:
                c = stack.pop()
                stack.extend(c.get("children") or [])
                user = c.get("user") or {}
                if (user.get("username") or "").lower() == me:
                    continue
                entries.append(
                    InboxEntry(
                        external_id=c["id_code"],
                        kind="comment",
                        text=html_to_text(c.get("body_html", "")),
                        url=f"{self.base}/{user.get('username', '')}/comment/{c['id_code']}",
                        author_name=user.get("name") or user.get("username", ""),
                        author_handle="@" + user.get("username", ""),
                        author_avatar=user.get("profile_image_90", ""),
                        author_url=f"{self.base}/{user.get('username', '')}",
                        created_at=parse_time(c.get("created_at")),
                        post_external_id=str(aid),
                    )
                )
        return entries, state

    def find_published(self, content, since):
        # Your articles, drafts included, newest first. Forem gives no creation time, so the
        # article must match the title and the text exactly.
        article = self._article(content)
        items = self.http(
            "GET", f"{self.base}/api/articles/me/all", headers=self._headers(), params={"page": 1, "per_page": 30}
        ).json()
        for a in items:
            if (
                a.get("title") == article["title"]
                and (a.get("body_markdown") or "").strip() == article["body_markdown"].strip()
            ):
                return PublishResult(external_id=str(a["id"]), url=a.get("url", ""))
        return None

    def delete(self, external_id):
        # Forem cannot delete through the API: unpublishing hides the article (it becomes a draft).
        self.http(
            "PUT",
            f"{self.base}/api/articles/{external_id}",
            headers=self._headers(),
            json={"article": {"published": False}},
        )

    # --- statistics -------------------------------------------------------------------------
    def fetch_metrics(self, external_ids):
        wanted = {str(i) for i in external_ids}
        out = {}
        for page in range(1, 11):  # up to 10 000 articles
            items = self.http(
                "GET",
                f"{self.base}/api/articles/me/all",
                headers=self._headers(),
                params={"page": page, "per_page": 1000},
            ).json()
            for a in items:
                if str(a.get("id")) in wanted:
                    out[str(a["id"])] = Metrics(
                        impressions=a.get("page_views_count"),
                        likes=a.get("public_reactions_count"),
                        comments=a.get("comments_count"),
                        raw={k: a.get(k) for k in ("page_views_count", "public_reactions_count", "comments_count")},
                    )
            if len(out) == len(wanted) or len(items) < 1000:
                break
        return out

    def fetch_audience(self):
        if self.channel.config.get("organization_id"):
            return None  # Forem does not expose organization followers.
        total = 0
        for page in range(1, 21):
            items = self.http(
                "GET",
                f"{self.base}/api/followers/users",
                headers=self._headers(),
                params={"page": page, "per_page": 1000},
            ).json()
            total += len(items)
            if len(items) < 1000:
                break
        return total

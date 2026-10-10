"""Medium via its legacy REST API and a self-issued integration token.

Medium stopped issuing new integration tokens in 2023 and no longer supports
the API, but existing tokens keep working. The API can only create posts: it
cannot edit, delete or read statistics.
"""

import re

from .base import BaseProvider, Candidate, Capabilities, CredentialField, ProviderError, PublishResult

API = "https://api.medium.com/v1"
MAX_TAGS = 3
MAX_TAG_LENGTH = 25


class MediumProvider(BaseProvider):
    key = "medium"
    label = "Medium"
    capabilities = Capabilities(
        kind="blog",
        max_chars=None,
        max_media=20,
        supports_video=False,
        supports_title=True,
        has_metrics=False,
        metric_names=(),
        auth="credentials",
    )
    credential_fields = [
        CredentialField(
            "integration_token",
            "Integration token",
            type="password",
            help="Medium → Settings → Security and apps → Integration tokens. Medium no longer issues new "
            "tokens, so this only works for accounts that already have one.",
        ),
        CredentialField(
            "default_status",
            "Publish as",
            type="select",
            required=False,
            default="public",
            choices=["public", "unlisted", "draft"],
        ),
        CredentialField("notify_followers", "Notify followers", type="checkbox", required=False, default=True),
    ]

    def _headers(self, token):
        return {"Authorization": f"Bearer {token}", "Accept": "application/json", "Accept-Charset": "utf-8"}

    def connect_with_credentials(self, data):
        token = data["integration_token"].strip()
        me = self.http("GET", f"{API}/me", headers=self._headers(token)).json()["data"]
        config = {
            "default_status": data.get("default_status") or "public",
            "notify_followers": bool(data.get("notify_followers", True)),
        }
        creds = {"integration_token": token}
        candidates = [
            Candidate(
                external_id=f"user:{me['id']}",
                name=me.get("name") or me["username"],
                handle=f"@{me['username']}",
                avatar_url=me.get("imageUrl", ""),
                profile_url=me.get("url", f"https://medium.com/@{me['username']}"),
                credentials=creds,
                config={**config, "author_id": me["id"], "target": "user"},
            )
        ]
        pubs = self.http("GET", f"{API}/users/{me['id']}/publications", headers=self._headers(token)).json()
        for pub in pubs.get("data", []):
            candidates.append(
                Candidate(
                    external_id=f"publication:{pub['id']}",
                    name=pub["name"],
                    handle=f"via @{me['username']}",
                    avatar_url=pub.get("imageUrl", ""),
                    profile_url=pub.get("url", ""),
                    credentials=creds,
                    config={**config, "author_id": me["id"], "publication_id": pub["id"], "target": "publication"},
                )
            )
        return candidates

    def validate(self, content):
        problems = super().validate(content)
        tags = content.options.get("tags") or content.tags
        if len(tags) > MAX_TAGS:
            problems.append(f"Medium accepts at most {MAX_TAGS} tags.")
        return problems

    def _upload(self, item):
        data = self.http(
            "POST",
            f"{API}/images",
            headers=self._headers(self.creds["integration_token"]),
            files={"image": (item.filename or "image", item.read(), item.mime)},
        ).json()["data"]
        return data["url"]

    def publish(self, content):
        cfg = self.channel.config
        opts = content.options
        headers = self._headers(self.creds["integration_token"])
        body = content.text
        for item in content.media:
            hosted = self._upload(item)
            if item.url in body:
                body = body.replace(item.url, hosted)
            else:
                # Images attached but not referenced in the text go after it.
                body += f"\n\n![{item.alt}]({hosted})"
        status = cfg.get("default_status", "public")
        if opts.get("status") == "draft":
            status = "draft"
        tags = [t[:MAX_TAG_LENGTH] for t in (opts.get("tags") or content.tags)][:MAX_TAGS]
        payload = {
            "title": content.title[:100],
            "contentFormat": "markdown",
            # Medium renders the title from the content's first heading.
            "content": f"# {content.title}\n\n{body}" if not re.match(r"^#\s", body) else body,
            "tags": tags,
            "publishStatus": status,
            "notifyFollowers": bool(cfg.get("notify_followers", True)),
        }
        canonical = opts.get("canonical_url") or content.link
        if canonical:
            payload["canonicalUrl"] = canonical
        if cfg.get("target") == "publication":
            url = f"{API}/publications/{cfg['publication_id']}/posts"
        else:
            url = f"{API}/users/{cfg['author_id']}/posts"
        post = self.http("POST", url, headers=headers, json=payload).json()["data"]
        return PublishResult(external_id=post["id"], url=post.get("url", ""))

    def delete(self, external_id):
        raise ProviderError("Medium's API cannot delete posts; remove it on medium.com.")

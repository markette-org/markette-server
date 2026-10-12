"""Mastodon (and compatible servers): one OAuth app registered per instance."""

import time
from dataclasses import replace
from urllib.parse import urlencode, urlparse

from django.conf import settings

from apps.platform import conf

from .base import (
    BaseProvider,
    Candidate,
    Capabilities,
    CredentialField,
    Metrics,
    ProviderError,
    PublishResult,
    redirect_uri,
    repeatable,
)

SCOPES = "read write"


def normalize_instance(url):
    url = (url or "").strip()
    if not url:
        raise ProviderError("Mastodon server URL is required.")
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ProviderError("Invalid Mastodon server URL.")
    return f"https://{parsed.netloc.lower()}"


class MastodonProvider(BaseProvider):
    key = "mastodon"
    label = "Mastodon"
    # Mastodon drops a repeated status with the same Idempotency-Key (kept for an hour).
    duplicate_guard = "idempotent"
    capabilities = Capabilities(
        max_chars=500,
        count_mode="mastodon",
        max_media=4,
        supports_threads=True,
        supports_edit=True,
        instance_url=True,
        metric_names=("likes", "comments", "shares"),
    )
    credential_fields = [CredentialField("base_url", "Server", type="url", help="e.g. mastodon.social")]

    # --- auth ------------------------------------------------------------
    def _app(self, base_url):
        from ..models import OAuthApp

        app = OAuthApp.objects.filter(provider=self.key, base_url=base_url).first()
        if app:
            return app
        resp = self.http(
            "POST",
            f"{base_url}/api/v1/apps",
            data={
                "client_name": conf.get("MASTODON_APP_NAME"),
                "redirect_uris": redirect_uri(self.key),
                "scopes": SCOPES,
                "website": settings.FRONTEND_URL,
            },
        ).json()
        app, _ = OAuthApp.objects.update_or_create(
            provider=self.key,
            base_url=base_url,
            defaults={"client_id": resp["client_id"], "secrets": {"client_secret": resp["client_secret"]}},
        )
        return app

    def authorization_url(self, request):
        base = normalize_instance(request.base_url)
        request.base_url = base
        request.save(update_fields=["base_url"])
        app = self._app(base)
        params = {
            "client_id": app.client_id,
            "redirect_uri": redirect_uri(self.key),
            "response_type": "code",
            "scope": SCOPES,
            "state": request.state,
        }
        return f"{base}/oauth/authorize?{urlencode(params)}"

    def complete_oauth(self, request, params):
        base = request.base_url
        app = self._app(base)
        tok = self.http(
            "POST",
            f"{base}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": params["code"],
                "client_id": app.client_id,
                "client_secret": app.secrets["client_secret"],
                "redirect_uri": redirect_uri(self.key),
                "scope": SCOPES,
            },
        ).json()
        token = tok["access_token"]
        acct = self.http(
            "GET", f"{base}/api/v1/accounts/verify_credentials", headers={"Authorization": f"Bearer {token}"}
        ).json()
        host = urlparse(base).netloc
        return [
            Candidate(
                external_id=acct["id"],
                name=acct.get("display_name") or acct["username"],
                handle=f"@{acct['username']}@{host}",
                avatar_url=acct.get("avatar", ""),
                profile_url=acct.get("url", ""),
                base_url=base,
                credentials={"access_token": token},
                followers_count=acct.get("followers_count"),
            )
        ]

    @property
    def base(self):
        return self.channel.base_url

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.creds['access_token']}"}

    # --- publishing ------------------------------------------------------
    @repeatable
    def _upload(self, item):
        resp = self.http(
            "POST",
            f"{self.base}/api/v2/media",
            headers=self.headers,
            files={"file": (item.filename or "media", item.read(), item.mime)},
            data={"description": item.alt[:1500]} if item.alt else None,
        )
        media = resp.json()
        # Large files are processed asynchronously (HTTP 202, url still null).
        for _ in range(60):
            if media.get("url"):
                return media["id"]
            time.sleep(2)
            r = self.http("GET", f"{self.base}/api/v1/media/{media['id']}", headers=self.headers, expect=(200, 206))
            media = r.json()
        raise ProviderError("Mastodon media processing timed out.", retryable=True)

    def publish(self, content):
        data = {"status": content.text, "visibility": content.options.get("visibility", "public")}
        if content.media:
            data["media_ids"] = [self._upload(m) for m in content.media]
        if cw := content.options.get("spoiler_text"):
            data["spoiler_text"] = cw
        if lang := content.options.get("language"):
            data["language"] = lang
        if parent := content.options.get("_in_reply_to_id"):
            data["in_reply_to_id"] = parent
        headers = {**self.headers}
        if content.idempotency_key:
            headers["Idempotency-Key"] = content.idempotency_key
        status = self.http("POST", f"{self.base}/api/v1/statuses", headers=headers, json=data).json()
        return PublishResult(external_id=status["id"], url=status.get("url") or status.get("uri", ""))

    def update(self, external_id, content, extra):
        current = self.http("GET", f"{self.base}/api/v1/statuses/{external_id}", headers=self.headers).json()
        data = {
            "status": content.text,
            # Without media_ids Mastodon would drop the attachments.
            "media_ids": [m["id"] for m in current.get("media_attachments") or []],
            "spoiler_text": content.options.get("spoiler_text", current.get("spoiler_text") or ""),
        }
        if lang := content.options.get("language"):
            data["language"] = lang
        self.http("PUT", f"{self.base}/api/v1/statuses/{external_id}", headers=self.headers, json=data)

    def publish_reply(self, content, parent, root):
        return self.publish(replace(content, options={**content.options, "_in_reply_to_id": parent.external_id}))

    def delete(self, external_id):
        self.http("DELETE", f"{self.base}/api/v1/statuses/{external_id}", headers=self.headers)

    # --- metrics ---------------------------------------------------------
    def fetch_metrics(self, external_ids):
        out = {}
        for sid in external_ids:
            try:
                st = self.http("GET", f"{self.base}/api/v1/statuses/{sid}", headers=self.headers).json()
            except ProviderError as exc:
                if exc.status == 404:
                    continue
                raise
            out[sid] = Metrics(
                likes=st.get("favourites_count"),
                comments=st.get("replies_count"),
                shares=st.get("reblogs_count"),
                raw={k: st.get(k) for k in ("favourites_count", "replies_count", "reblogs_count")},
            )
        return out

    def fetch_audience(self):
        acct = self.http("GET", f"{self.base}/api/v1/accounts/verify_credentials", headers=self.headers).json()
        return acct.get("followers_count")

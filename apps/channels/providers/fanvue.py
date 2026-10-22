"""Fanvue (creator subscriptions) through its official API: OAuth 2.0 with PKCE, posts, media, comments.

Needs a Fanvue app (only KYC-verified creators can create one) with the redirect URI
``{PUBLIC_API_URL}/api/oauth/fanvue/callback``; set FANVUE_CLIENT_ID / FANVUE_CLIENT_SECRET.
Media goes up in parts (POST /v1/media/uploads → signed part URLs → PATCH to complete) and is
attached once Fanvue has processed it. A post has an audience (subscribers, or followers and
subscribers) and, with media, an optional price (pay to unlock). Adult category: only in
workspaces with adult content turned on.
"""

import math
import time
from datetime import timedelta
from urllib.parse import urlencode

from django.utils import timezone

from apps.platform import conf

from .base import (
    BaseProvider,
    Candidate,
    Capabilities,
    HistoryEntry,
    InboxEntry,
    Metrics,
    ProviderError,
    PublishResult,
    pkce_pair,
    redirect_uri,
    repeatable,
)
from .text import parse_time

AUTHORIZE = "https://auth.fanvue.com/oauth2/auth"
TOKEN = "https://auth.fanvue.com/oauth2/token"
API = "https://api.fanvue.com/v1"
VERSION = "2025-06-26"
SCOPES = "openid offline_access offline read:self read:post write:post read:media write:media"
AUDIENCES = ("subscribers", "followers-and-subscribers")


class FanvueProvider(BaseProvider):
    key = "fanvue"
    label = "Fanvue"
    capabilities = Capabilities(
        kind="social",
        adult=True,
        max_chars=5000,
        max_media=20,
        supports_edit=True,
        inbox=True,
        inbox_interval=10,
        inbox_reply=True,
        history=True,
        metric_names=("likes", "comments"),
    )

    @classmethod
    def is_configured(cls):
        return bool(conf.get("FANVUE_CLIENT_ID") and conf.get("FANVUE_CLIENT_SECRET"))

    # --- auth ----------------------------------------------------------------------
    def authorization_url(self, request):
        verifier, challenge = pkce_pair()
        request.secrets = {"code_verifier": verifier}
        request.save(update_fields=["secrets"])
        params = {
            "response_type": "code",
            "client_id": conf.get("FANVUE_CLIENT_ID"),
            "redirect_uri": redirect_uri(self.key),
            "scope": SCOPES,
            "state": request.state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE}?{urlencode(params)}"

    def _token(self, data):
        # Fanvue clients use client_secret_basic: the credentials go in the Authorization header only.
        tok = self.http(
            "POST", TOKEN, data=data, auth=(conf.get("FANVUE_CLIENT_ID"), conf.get("FANVUE_CLIENT_SECRET"))
        ).json()
        return (
            {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", "")},
            timezone.now() + timedelta(seconds=int(tok.get("expires_in") or 3600)),
        )

    def complete_oauth(self, request, params):
        creds, expires = self._token(
            {
                "grant_type": "authorization_code",
                "code": params["code"],
                "redirect_uri": redirect_uri(self.key),
                "code_verifier": request.secrets["code_verifier"],
            }
        )
        me = self.http("GET", f"{API}/users/me", headers=self._headers(creds["access_token"])).json()
        if not me.get("isCreator"):
            raise ProviderError("This Fanvue account is not a creator account, so it cannot publish posts.")
        return [
            Candidate(
                external_id=me["uuid"],
                name=me.get("displayName") or me.get("handle", ""),
                handle=f"@{me.get('handle', '')}",
                avatar_url=me.get("avatarUrl") or "",
                profile_url=f"https://www.fanvue.com/{me.get('handle', '')}",
                credentials=creds,
                token_expires_at=expires,
                config={"handle": me.get("handle", ""), "ai_creator": bool(me.get("isAiCreator"))},
            )
        ]

    def refresh_credentials(self):
        exp = self.channel.token_expires_at
        if exp and exp - timezone.now() > timedelta(minutes=5):
            return False
        if not self.creds.get("refresh_token"):
            raise ProviderError("Fanvue access expired; reconnect the channel.", auth=True)
        # Refresh tokens rotate and work once: store the new one right away.
        creds, expires = self._token({"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"]})
        self.save_credentials(creds, expires)
        return True

    def _headers(self, token=None):
        return {"Authorization": f"Bearer {token or self.creds['access_token']}", "X-Fanvue-API-Version": VERSION}

    def api(self, method, path, **kwargs):
        return self.http(method, f"{API}{path}", headers=self._headers(), **kwargs)

    # --- media ---------------------------------------------------------------------
    @repeatable
    def _upload(self, item):
        kind = "video" if item.is_video else "image"
        size = item.path.stat().st_size
        session = self.api(
            "POST",
            "/media/uploads",
            json={
                "name": item.filename or "upload",
                "filename": item.filename or "upload",
                "mediaType": kind,
                "sizeBytes": size,
            },
        ).json()
        total = session.get("totalParts") or math.ceil(size / session["partSize"])
        signed = self.api(
            "GET", f"/media/uploads/{session['uploadId']}/parts/urls", params={"from": 1, "to": total}
        ).json()
        parts = []
        with item.path.open("rb") as fh:
            for part in signed["parts"]:
                fh.seek((part["partNumber"] - 1) * session["partSize"])
                chunk = fh.read(session["partSize"])
                resp = self.http("PUT", part["url"], content=chunk)  # signed URL: no Authorization header
                parts.append({"PartNumber": part["partNumber"], "ETag": resp.headers.get("etag", "")})
        self.api("PATCH", f"/media/uploads/{session['uploadId']}", json={"parts": parts})
        # Attach only once processed, or fans get media they cannot open yet.
        for _ in range(200):
            media = self.api("GET", f"/media/{session['mediaUuid']}").json()
            if media.get("status") == "ready":
                return session["mediaUuid"]
            if media.get("status") == "error":
                raise ProviderError(
                    f"Fanvue could not process {item.filename or 'the file'}: {media.get('errorReason')}"
                )
            time.sleep(3)
        raise ProviderError("Fanvue is still processing the media; trying again later.", retryable=True)

    # --- publishing ----------------------------------------------------------------
    def validate(self, content):
        problems = super().validate(content)
        opts = content.options or {}
        if opts.get("audience") and opts["audience"] not in AUDIENCES:
            problems.append("The audience is subscribers, or followers and subscribers.")
        price = opts.get("price")
        if price not in (None, ""):
            try:
                cents = round(float(price) * 100)
            except (TypeError, ValueError):
                cents = 0
            if cents < 300:
                problems.append("Fanvue prices start at 3.00.")
            if not content.media:
                problems.append("A paid Fanvue post needs media.")
        return problems

    def _body(self, content):
        opts = content.options or {}
        body = {"text": content.text, "audience": opts.get("audience") or "subscribers"}
        if opts.get("price") not in (None, ""):
            body["price"] = round(float(opts["price"]) * 100)
        return body

    def _url(self):
        return f"https://www.fanvue.com/{(self.channel.config or {}).get('handle', '')}"

    def publish(self, content):
        body = self._body(content)
        if content.media:
            body["mediaUuids"] = [self._upload(m) for m in content.media]
        post = self.api("POST", "/posts", json=body).json()
        # Fanvue's API gives no public address for a post: link the creator's page.
        return PublishResult(external_id=post["uuid"], url=self._url())

    def update(self, external_id, content, extra):
        self.api("PATCH", f"/posts/{external_id}", json={"text": content.text})

    def delete(self, external_id):
        self.api("DELETE", f"/posts/{external_id}")

    def fetch_metrics(self, external_ids):
        out = {}
        for uuid in external_ids:
            try:
                p = self.api("GET", f"/posts/{uuid}").json()
            except ProviderError as exc:
                if exc.status == 404:
                    continue
                raise
            out[uuid] = Metrics(
                likes=p.get("likesCount"),
                comments=p.get("commentsCount"),
                raw={"tips": p.get("tips")},
            )
        return out

    def fetch_history(self, limit=100):
        out, cursor = [], None
        while len(out) < limit:
            params = {"size": 50}
            if cursor:
                params["cursor"] = cursor
            page = self.api("GET", "/posts", params=params).json()
            for p in page.get("data", []):
                when = parse_time(p.get("publishedAt") or p.get("createdAt"))
                if when:
                    out.append(
                        HistoryEntry(
                            external_id=p["uuid"],
                            published_at=when,
                            text=p.get("text") or "",
                            url=self._url(),
                            metrics=Metrics(likes=p.get("likesCount"), comments=p.get("commentsCount")),
                        )
                    )
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return out[:limit]

    # --- inbox: comments on recent posts --------------------------------------------
    def fetch_inbox(self, state, recent_posts):
        me = self.channel.external_id
        entries = []
        for uuid in recent_posts[:15]:
            page = self.api("GET", f"/posts/{uuid}/comments", params={"size": 50}).json()
            for c in page.get("data", []):
                user = c.get("user") or {}
                if user.get("uuid") == me:
                    continue
                entries.append(
                    InboxEntry(
                        external_id=c["uuid"],
                        kind="comment",
                        text=c.get("text") or "",
                        url=self._url(),
                        author_name=user.get("displayName") or user.get("nickname") or user.get("handle", ""),
                        author_handle=f"@{user['handle']}" if user.get("handle") else "",
                        created_at=parse_time(c.get("createdAt")),
                        post_external_id=uuid,
                        context={"post": uuid, "top_spender": bool(user.get("isTopSpender"))},
                    )
                )
        return entries, state

    def reply_inbox(self, entry, text):
        # The API comments on the post (it has no threaded replies); address the fan by name.
        handle = entry.author_handle
        if handle and handle.lower() not in text.lower():
            text = f"{handle} {text}"
        c = self.api("POST", f"/posts/{entry.context['post']}/comments", json={"text": text[:600]}).json()
        return PublishResult(external_id=c["uuid"], url=self._url())

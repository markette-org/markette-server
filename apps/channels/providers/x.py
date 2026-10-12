"""X (Twitter) via API v2 with OAuth 2.0 user context (PKCE)."""

from dataclasses import replace
from datetime import timedelta
from urllib.parse import urlencode

from django.utils import timezone

from apps.platform import conf

from .base import (
    BaseProvider,
    Candidate,
    Capabilities,
    Content,
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

API = "https://api.x.com/2"
AUTHORIZE = "https://x.com/i/oauth2/authorize"
TOKEN = "https://api.x.com/2/oauth2/token"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"


class XProvider(BaseProvider):
    key = "x"
    label = "X"
    capabilities = Capabilities(
        max_chars=280,
        count_mode="x",
        max_media=4,
        supports_threads=True,
        # Mentions need X's paid API access; the free tier answers 403 and the inbox says so.
        inbox=True,
        inbox_interval=15,
        inbox_reply=True,
        history=True,
        metric_names=("impressions", "likes", "comments", "shares", "saves"),
    )

    @classmethod
    def is_configured(cls):
        return bool(conf.get("X_CLIENT_ID"))

    # --- auth ------------------------------------------------------------
    def authorization_url(self, request):
        verifier, challenge = pkce_pair()
        request.secrets = {"code_verifier": verifier}
        request.save(update_fields=["secrets"])
        params = {
            "response_type": "code",
            "client_id": conf.get("X_CLIENT_ID"),
            "redirect_uri": redirect_uri(self.key),
            "scope": SCOPES,
            "state": request.state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE}?{urlencode(params)}"

    def _token(self, data):
        auth = (conf.get("X_CLIENT_ID"), conf.get("X_CLIENT_SECRET")) if conf.get("X_CLIENT_SECRET") else None
        if not auth:
            data = {**data, "client_id": conf.get("X_CLIENT_ID")}
        resp = self.http("POST", TOKEN, data=data, auth=auth)
        tok = resp.json()
        return {
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token", ""),
        }, timezone.now() + timedelta(seconds=int(tok.get("expires_in", 7200)))

    def complete_oauth(self, request, params):
        creds, expires = self._token(
            {
                "grant_type": "authorization_code",
                "code": params["code"],
                "redirect_uri": redirect_uri(self.key),
                "code_verifier": request.secrets["code_verifier"],
            }
        )
        me = self.http(
            "GET",
            f"{API}/users/me",
            params={"user.fields": "profile_image_url,public_metrics,username,name"},
            headers={"Authorization": f"Bearer {creds['access_token']}"},
        ).json()["data"]
        return [
            Candidate(
                external_id=me["id"],
                name=me["name"],
                handle=f"@{me['username']}",
                avatar_url=me.get("profile_image_url", ""),
                profile_url=f"https://x.com/{me['username']}",
                credentials=creds,
                token_expires_at=expires,
                followers_count=me.get("public_metrics", {}).get("followers_count"),
            )
        ]

    def refresh_credentials(self):
        exp = self.channel.token_expires_at
        if exp and exp - timezone.now() > timedelta(minutes=10):
            return False
        if not self.creds.get("refresh_token"):
            raise ProviderError("X token expired and no refresh token is stored.", auth=True)
        creds, expires = self._token({"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"]})
        self.save_credentials(creds, expires)
        return True

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.creds['access_token']}"}

    # --- publishing ------------------------------------------------------
    @repeatable
    def _upload(self, item):
        category = "tweet_video" if item.is_video else ("tweet_gif" if item.mime == "image/gif" else "tweet_image")
        if not item.is_video:
            resp = self.http(
                "POST",
                f"{API}/media/upload",
                headers=self.headers,
                files={"media": (item.filename or "media", item.read(), item.mime)},
                data={"media_category": category, "media_type": item.mime},
            )
            media_id = resp.json()["data"]["id"]
        else:
            media_id = self._upload_chunked(item, category)
        if item.alt:
            self.http(
                "POST",
                f"{API}/media/metadata",
                headers=self.headers,
                json={"id": media_id, "metadata": {"alt_text": {"text": item.alt[:1000]}}},
            )
        return media_id

    @repeatable
    def _upload_chunked(self, item, category):
        import time

        init = self.http(
            "POST",
            f"{API}/media/upload/initialize",
            headers=self.headers,
            json={
                "media_type": item.mime,
                "total_bytes": item.size,
                "media_category": category,
            },
        ).json()["data"]
        media_id = init["id"]
        chunk = 4 * 1024 * 1024
        with item.path.open("rb") as fh:
            index = 0
            while data := fh.read(chunk):
                self.http(
                    "POST",
                    f"{API}/media/upload/{media_id}/append",
                    headers=self.headers,
                    files={"media": ("chunk", data, "application/octet-stream")},
                    data={"segment_index": str(index)},
                )
                index += 1
        info = self.http("POST", f"{API}/media/upload/{media_id}/finalize", headers=self.headers).json()["data"]
        for _ in range(60):
            state = (info.get("processing_info") or {}).get("state")
            if state in (None, "succeeded"):
                return media_id
            if state == "failed":
                raise ProviderError(f"X could not process the video: {info['processing_info']}")
            time.sleep(info["processing_info"].get("check_after_secs") or 3)
            info = self.http(
                "GET", f"{API}/media/upload", headers=self.headers, params={"command": "STATUS", "media_id": media_id}
            ).json()["data"]
        raise ProviderError("X video processing timed out.", retryable=True)

    def publish(self, content: Content):
        body = {"text": content.text}
        if content.media:
            body["media"] = {"media_ids": [self._upload(m) for m in content.media]}
        if reply_to := content.options.get("reply_to_id"):
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        data = self.http("POST", f"{API}/tweets", headers=self.headers, json=body).json()["data"]
        username = (self.channel.handle or "i").lstrip("@")
        return PublishResult(external_id=data["id"], url=f"https://x.com/{username}/status/{data['id']}")

    def fetch_inbox(self, state, recent_posts):
        params = {
            "max_results": 50,
            "expansions": "author_id",
            "tweet.fields": "created_at,referenced_tweets",
            "user.fields": "name,username,profile_image_url",
        }
        if state.get("since_id"):
            params["since_id"] = state["since_id"]
        res = self.http(
            "GET", f"{API}/users/{self.channel.external_id}/mentions", headers=self.headers, params=params
        ).json()
        users = {u["id"]: u for u in (res.get("includes") or {}).get("users", [])}
        entries = []
        for t in res.get("data", []):
            u = users.get(t.get("author_id"), {})
            replied = next((r["id"] for r in t.get("referenced_tweets") or [] if r.get("type") == "replied_to"), "")
            entries.append(
                InboxEntry(
                    external_id=t["id"],
                    kind="reply" if replied else "mention",
                    text=t.get("text", ""),
                    url=f"https://x.com/{u.get('username', 'i')}/status/{t['id']}",
                    author_name=u.get("name", ""),
                    author_handle="@" + u.get("username", ""),
                    author_avatar=u.get("profile_image_url", ""),
                    author_url=f"https://x.com/{u.get('username', '')}",
                    created_at=parse_time(t.get("created_at")),
                    post_external_id=replied,
                )
            )
        newest = (res.get("meta") or {}).get("newest_id")
        return entries, ({**state, "since_id": newest} if newest else state)

    def reply_inbox(self, entry, text):
        return self.publish(Content(text=text, options={"reply_to_id": entry.external_id}))

    def publish_reply(self, content, parent, root):
        return self.publish(replace(content, options={**content.options, "reply_to_id": parent.external_id}))

    def delete(self, external_id):
        self.http("DELETE", f"{API}/tweets/{external_id}", headers=self.headers)

    # --- metrics ---------------------------------------------------------
    def fetch_history(self, limit=100):
        out, token = [], None
        username = (self.channel.handle or "i").lstrip("@")
        while len(out) < limit:
            params = {"max_results": 100, "exclude": "replies,retweets", "tweet.fields": "created_at,public_metrics"}
            if token:
                params["pagination_token"] = token
            res = self.http(
                "GET", f"{API}/users/{self.channel.external_id}/tweets", headers=self.headers, params=params
            ).json()
            for tw in res.get("data", []):
                pm = tw.get("public_metrics", {})
                out.append(
                    HistoryEntry(
                        external_id=tw["id"],
                        published_at=parse_time(tw.get("created_at")),
                        text=tw.get("text", ""),
                        url=f"https://x.com/{username}/status/{tw['id']}",
                        metrics=Metrics(
                            impressions=pm.get("impression_count"),
                            likes=pm.get("like_count"),
                            comments=pm.get("reply_count"),
                            shares=(pm.get("retweet_count") or 0) + (pm.get("quote_count") or 0),
                            saves=pm.get("bookmark_count"),
                        ),
                    )
                )
            token = (res.get("meta") or {}).get("next_token")
            if not token:
                break
        return out[:limit]

    def fetch_metrics(self, external_ids):
        out = {}
        for i in range(0, len(external_ids), 100):
            batch = external_ids[i : i + 100]
            resp = self.http(
                "GET",
                f"{API}/tweets",
                headers=self.headers,
                params={
                    "ids": ",".join(batch),
                    "tweet.fields": "public_metrics,non_public_metrics,created_at",
                },
            ).json()
            for tw in resp.get("data", []):
                pm = tw.get("public_metrics", {})
                npm = tw.get("non_public_metrics", {})
                out[tw["id"]] = Metrics(
                    impressions=pm.get("impression_count") or npm.get("impression_count"),
                    likes=pm.get("like_count"),
                    comments=pm.get("reply_count"),
                    shares=(pm.get("retweet_count") or 0) + (pm.get("quote_count") or 0),
                    saves=pm.get("bookmark_count"),
                    clicks=npm.get("url_link_clicks"),
                    raw={"public_metrics": pm, "non_public_metrics": npm},
                )
        return out

    def fetch_audience(self):
        me = self.http("GET", f"{API}/users/me", headers=self.headers, params={"user.fields": "public_metrics"}).json()[
            "data"
        ]
        return me.get("public_metrics", {}).get("followers_count")

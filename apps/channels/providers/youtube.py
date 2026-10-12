"""YouTube video uploads (including Shorts) through the YouTube Data API v3.

Google OAuth with the youtube.upload and youtube.readonly scopes. Important: videos
uploaded from Google Cloud projects that have not passed YouTube's API audit are forced
to *private*, whatever privacy is requested. Request the audit for public uploads.
Shorts are regular uploads that are vertical and up to three minutes long.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.utils import timezone

from apps.platform import conf

from .base import BaseProvider, Candidate, Capabilities, InboxEntry, Metrics, ProviderError, PublishResult, redirect_uri
from .text import parse_time

AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
# youtube.force-ssl lets Markette answer comments from the inbox.
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly "
    "https://www.googleapis.com/auth/youtube.force-ssl"
)
CHUNK = 8 * 1024 * 1024


class YouTubeProvider(BaseProvider):
    key = "youtube"
    label = "YouTube"
    capabilities = Capabilities(
        max_chars=5000,  # description
        max_media=1,
        requires_media=True,
        supports_title=True,
        inbox=True,
        inbox_interval=30,  # comment reads cost quota
        inbox_reply=True,
        metric_names=("impressions", "likes", "comments"),
    )

    @classmethod
    def is_configured(cls):
        return bool(conf.get("GOOGLE_CLIENT_ID") and conf.get("GOOGLE_CLIENT_SECRET"))

    def authorization_url(self, request):
        params = {
            "client_id": conf.get("GOOGLE_CLIENT_ID"),
            "redirect_uri": redirect_uri(self.key),
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": request.state,
        }
        return f"{AUTHORIZE}?{urlencode(params)}"

    def _token(self, data):
        tok = self.http(
            "POST",
            TOKEN,
            data={**data, "client_id": conf.get("GOOGLE_CLIENT_ID"), "client_secret": conf.get("GOOGLE_CLIENT_SECRET")},
        ).json()
        return tok, timezone.now() + timedelta(seconds=int(tok.get("expires_in", 3600)))

    def complete_oauth(self, request, params):
        tok, expires = self._token(
            {"grant_type": "authorization_code", "code": params["code"], "redirect_uri": redirect_uri(self.key)}
        )
        headers = {"Authorization": f"Bearer {tok['access_token']}"}
        items = (
            self.http("GET", f"{API}/channels", headers=headers, params={"mine": "true", "part": "snippet,statistics"})
            .json()
            .get("items", [])
        )
        if not items:
            raise ProviderError("This Google account has no YouTube channel.")
        creds = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", "")}
        return [
            Candidate(
                external_id=ch["id"],
                name=ch["snippet"]["title"],
                handle=ch["snippet"].get("customUrl", ""),
                avatar_url=((ch["snippet"].get("thumbnails") or {}).get("default") or {}).get("url", ""),
                profile_url=f"https://www.youtube.com/channel/{ch['id']}",
                credentials=creds,
                token_expires_at=expires,
                followers_count=int((ch.get("statistics") or {}).get("subscriberCount", 0) or 0),
                config={"privacy": "public", "category_id": "22"},
            )
            for ch in items
        ]

    def refresh_credentials(self):
        exp = self.channel.token_expires_at
        if exp and exp - timezone.now() > timedelta(minutes=10):
            return False
        if not self.creds.get("refresh_token"):
            raise ProviderError("YouTube access expired; reconnect the channel.", auth=True)
        tok, expires = self._token({"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"]})
        self.save_credentials({**self.creds, "access_token": tok["access_token"]}, expires)
        return True

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.creds['access_token']}"}

    def error_from_response(self, resp):
        err = super().error_from_response(resp)
        try:
            reason = (resp.json().get("error", {}).get("errors") or [{}])[0].get("reason", "")
        except (ValueError, AttributeError):
            return err
        if reason in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded"):
            err.retryable, err.retry_after = True, 6 * 3600
            err.args = (f"YouTube upload quota reached ({reason}); it resets daily.",)
        return err

    # --- publishing ---------------------------------------------------------------------
    def validate(self, content):
        problems = super().validate(content)
        if content.media and not content.media[0].is_video:
            problems.append("YouTube needs a video.")
        if len(content.title) > 100:
            problems.append("YouTube titles are limited to 100 characters.")
        if "<" in content.title or ">" in content.title:
            problems.append("YouTube titles cannot contain < or >.")
        return problems

    def publish(self, content):
        video = content.media[0]
        opts, cfg = content.options, self.channel.config
        tags = (opts.get("tags") or content.tags)[:30]
        metadata = {
            "snippet": {
                "title": content.title[:100],
                "description": content.text[:5000],
                "tags": tags,
                "categoryId": str(opts.get("category_id") or cfg.get("category_id", "22")),
            },
            "status": {
                "privacyStatus": opts.get("privacy") or cfg.get("privacy", "public"),
                "selfDeclaredMadeForKids": bool(opts.get("made_for_kids", False)),
                "embeddable": True,
            },
        }
        init = self.http(
            "POST",
            UPLOAD,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={**self.headers, "X-Upload-Content-Type": video.mime, "X-Upload-Content-Length": str(video.size)},
            json=metadata,
        )
        location = init.headers["Location"]
        result = None
        with video.path.open("rb") as fh:
            offset = 0
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                end = offset + len(chunk) - 1
                resp = self.http(
                    "PUT",
                    location,
                    content=chunk,
                    expect=(200, 201, 308),
                    headers={"Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{video.size}"},
                )
                offset = end + 1
                if resp.status_code in (200, 201):
                    result = resp.json()
        if result is None:
            raise ProviderError("YouTube did not confirm the upload.", retryable=True)
        vid = result["id"]
        privacy = (result.get("status") or {}).get("privacyStatus")
        extra = {"privacy": privacy}
        if privacy == "private" and metadata["status"]["privacyStatus"] != "private":
            extra["note"] = "YouTube kept the video private: the Google Cloud project has not passed the API audit."
        return PublishResult(external_id=vid, url=f"https://www.youtube.com/watch?v={vid}", extra=extra)

    def delete(self, external_id):
        self.http("DELETE", f"{API}/videos", headers=self.headers, params={"id": external_id})

    def fetch_inbox(self, state, recent_posts):
        entries = []
        for vid in recent_posts[:10]:
            res = self.http(
                "GET",
                f"{API}/commentThreads",
                headers=self.headers,
                params={
                    "part": "snippet",
                    "videoId": vid,
                    "order": "time",
                    "maxResults": 50,
                    "textFormat": "plainText",
                },
            ).json()
            for thread in res.get("items", []):
                top = (thread.get("snippet") or {}).get("topLevelComment") or {}
                sn = top.get("snippet") or {}
                if sn.get("authorChannelId", {}).get("value") == self.channel.external_id:
                    continue
                entries.append(
                    InboxEntry(
                        external_id=top["id"],
                        kind="comment",
                        text=sn.get("textOriginal") or sn.get("textDisplay", ""),
                        url=f"https://www.youtube.com/watch?v={vid}&lc={top['id']}",
                        author_name=sn.get("authorDisplayName", ""),
                        author_avatar=sn.get("authorProfileImageUrl", ""),
                        author_url=sn.get("authorChannelUrl", ""),
                        created_at=parse_time(sn.get("publishedAt")),
                        post_external_id=vid,
                    )
                )
        return entries, state

    def reply_inbox(self, entry, text):
        res = self.http(
            "POST",
            f"{API}/comments",
            headers=self.headers,
            params={"part": "snippet"},
            json={"snippet": {"parentId": entry.external_id, "textOriginal": text}},
        ).json()
        return PublishResult(external_id=res["id"], url="")

    def fetch_metrics(self, external_ids):
        out = {}
        for i in range(0, len(external_ids), 50):
            batch = external_ids[i : i + 50]
            items = (
                self.http(
                    "GET", f"{API}/videos", headers=self.headers, params={"id": ",".join(batch), "part": "statistics"}
                )
                .json()
                .get("items", [])
            )
            for it in items:
                st = it.get("statistics", {})
                out[it["id"]] = Metrics(
                    impressions=int(st.get("viewCount", 0)),
                    likes=int(st.get("likeCount", 0)),
                    comments=int(st.get("commentCount", 0)),
                    video_views=int(st.get("viewCount", 0)),
                    raw=st,
                )
        return out

    def fetch_audience(self):
        items = (
            self.http(
                "GET",
                f"{API}/channels",
                headers=self.headers,
                params={"id": self.channel.external_id, "part": "statistics"},
            )
            .json()
            .get("items", [])
        )
        return int(items[0]["statistics"].get("subscriberCount", 0)) if items else None

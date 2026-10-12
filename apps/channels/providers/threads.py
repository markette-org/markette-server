"""Threads (Meta) via the Threads API: text, image, video and carousel posts, insights.

Requires a Meta app with the Threads use case (its own Threads App ID/Secret) and the
permissions threads_basic, threads_content_publish and threads_manage_insights
(threads_manage_replies for first comments, which are posted as replies).
"""

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
    redirect_uri,
    repeatable,
)
from .text import URL_RE, parse_time

API = "https://graph.threads.net/v1.0"
AUTHORIZE = "https://threads.com/oauth/authorize"
SCOPES = "threads_basic,threads_content_publish,threads_manage_insights,threads_manage_replies,threads_read_replies"
MAX_LINKS = 5


class ThreadsProvider(BaseProvider):
    key = "threads"
    label = "Threads"
    capabilities = Capabilities(
        max_chars=500,
        max_media=20,
        supports_threads=True,
        supports_first_comment=True,
        history=True,
        inbox=True,
        inbox_interval=10,
        inbox_reply=True,
        first_comment_max=500,
        metric_names=("impressions", "likes", "comments", "shares"),
    )

    @classmethod
    def is_configured(cls):
        return bool(conf.get("THREADS_APP_ID") and conf.get("THREADS_APP_SECRET"))

    def authorization_url(self, request):
        params = {
            "client_id": conf.get("THREADS_APP_ID"),
            "redirect_uri": redirect_uri(self.key),
            "scope": SCOPES,
            "response_type": "code",
            "state": request.state,
        }
        return f"{AUTHORIZE}?{urlencode(params)}"

    def complete_oauth(self, request, params):
        short = self.http(
            "POST",
            "https://graph.threads.net/oauth/access_token",
            data={
                "client_id": conf.get("THREADS_APP_ID"),
                "client_secret": conf.get("THREADS_APP_SECRET"),
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri(self.key),
                "code": params["code"],
            },
        ).json()
        long = self.http(
            "GET",
            "https://graph.threads.net/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": conf.get("THREADS_APP_SECRET"),
                "access_token": short["access_token"],
            },
        ).json()
        token = long["access_token"]
        me = self.http(
            "GET", f"{API}/me", params={"fields": "id,username,name,threads_profile_picture_url", "access_token": token}
        ).json()
        return [
            Candidate(
                external_id=str(me["id"]),
                name=me.get("name") or me["username"],
                handle=f"@{me['username']}",
                avatar_url=me.get("threads_profile_picture_url", ""),
                profile_url=f"https://www.threads.com/@{me['username']}",
                credentials={"access_token": token},
                token_expires_at=timezone.now() + timedelta(seconds=int(long.get("expires_in", 5_184_000))),
            )
        ]

    def refresh_credentials(self):
        exp = self.channel.token_expires_at
        if exp and exp - timezone.now() > timedelta(days=10):
            return False
        data = self.http(
            "GET",
            "https://graph.threads.net/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": self.creds["access_token"]},
        ).json()
        self.save_credentials(
            {"access_token": data["access_token"]},
            timezone.now() + timedelta(seconds=int(data.get("expires_in", 5_184_000))),
        )
        return True

    def error_from_response(self, resp):
        err = super().error_from_response(resp)
        try:
            e = resp.json().get("error", {})
        except ValueError:
            return err
        if e.get("code") == 190:
            err.auth = True
        elif e.get("code") in (4, 17, 32, 613):
            err.retryable, err.retry_after = True, err.retry_after or 900
        return err

    def _g(self, method, path, **params):
        params["access_token"] = self.creds["access_token"]
        if method == "GET":
            return self.http(method, f"{API}/{path}", params=params).json()
        return self.http(method, f"{API}/{path}", data=params).json()

    # --- publishing -----------------------------------------------------------------
    def validate(self, content):
        from copy import deepcopy

        problems = super().validate(content)
        posted, _ = self.prepare_first_comment(deepcopy(content))  # links moved to the reply don't count
        if len(set(URL_RE.findall(posted.text))) > MAX_LINKS:
            problems.append(f"Threads allows at most {MAX_LINKS} different links per post.")
        return problems

    def _wait(self, container):
        for _ in range(40):
            st = self._g("GET", container, fields="status,error_message")
            if st.get("status") in ("FINISHED", "PUBLISHED"):
                return
            if st.get("status") in ("ERROR", "EXPIRED"):
                raise ProviderError(f"Threads could not process the media: {st.get('error_message') or st}")
            time.sleep(3)
        raise ProviderError("Threads media processing timed out.", retryable=True)

    @repeatable
    def _container(self, item=None, text=None, carousel_item=False, reply_to=None, link=None):
        uid = self.channel.external_id
        params = {}
        if item is None:
            params.update(media_type="TEXT", text=text or "")
            if link:
                params["link_attachment"] = link
        elif item.is_video:
            params.update(media_type="VIDEO", video_url=item.url)
        else:
            params.update(media_type="IMAGE", image_url=item.url)
            if item.alt:
                params["alt_text"] = item.alt[:1000]
        if item is not None and text is not None:
            params["text"] = text
        if carousel_item:
            params["is_carousel_item"] = "true"
        if reply_to:
            params["reply_to_id"] = reply_to
        cid = self._g("POST", f"{uid}/threads", **params)["id"]
        if item is not None and item.is_video:
            self._wait(cid)
        return cid

    def _publish_container(self, creation_id):
        uid = self.channel.external_id
        for attempt in range(3):
            try:
                return self._g("POST", f"{uid}/threads_publish", creation_id=creation_id)["id"]
            except ProviderError as exc:
                if attempt == 2 or exc.status != 400:
                    raise
                time.sleep(10)  # images may still be processing

    def publish(self, content):
        text = content.text.strip()
        if not content.media:
            creation = self._container(text=text, link=content.link or None)
        elif len(content.media) == 1:
            creation = self._container(content.media[0], text=text)
        else:
            children = [self._container(m, carousel_item=True) for m in content.media[:20]]
            creation = self._g(
                "POST",
                f"{self.channel.external_id}/threads",
                media_type="CAROUSEL",
                children=",".join(children),
                text=text,
            )["id"]
            self._wait(creation)
        media_id = self._publish_container(creation)
        permalink = self._g("GET", media_id, fields="permalink").get("permalink", "")
        return PublishResult(external_id=media_id, url=permalink)

    def fetch_history(self, limit=100):
        out, after = [], None
        while len(out) < limit:
            params = {"fields": "id,text,timestamp,permalink,is_quote_post", "limit": 100}
            if after:
                params["after"] = after
            res = self._g("GET", f"{self.channel.external_id}/threads", **params)
            for p in res.get("data", []):
                out.append(
                    HistoryEntry(
                        external_id=p["id"],
                        published_at=parse_time(p.get("timestamp")),
                        text=p.get("text", ""),
                        url=p.get("permalink", ""),
                    )
                )
            after = ((res.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not res.get("data") or not (res.get("paging") or {}).get("next"):
                break
        return out[:limit]

    def fetch_inbox(self, state, recent_posts):
        own = (self.channel.handle or "").lstrip("@").lower()
        entries = []
        for mid in recent_posts[:15]:
            res = self._g("GET", f"{mid}/replies", fields="id,text,username,timestamp,permalink", reverse="false")
            for r in res.get("data", []):
                if (r.get("username") or "").lower() == own:
                    continue
                entries.append(
                    InboxEntry(
                        external_id=r["id"],
                        kind="reply",
                        text=r.get("text", ""),
                        url=r.get("permalink", ""),
                        author_name=r.get("username", ""),
                        author_handle="@" + r.get("username", ""),
                        author_url=f"https://www.threads.com/@{r.get('username', '')}",
                        created_at=parse_time(r.get("timestamp")),
                        post_external_id=mid,
                    )
                )
        return entries, state

    def reply_inbox(self, entry, text):
        media_id = self._publish_container(self._container(text=text, reply_to=entry.external_id))
        return PublishResult(external_id=media_id, url="")

    def publish_reply(self, content, parent, root):
        media_id = self._publish_container(self._container(text=content.text.strip(), reply_to=parent.external_id))
        permalink = self._g("GET", media_id, fields="permalink").get("permalink", "")
        return PublishResult(external_id=media_id, url=permalink)

    def post_first_comment(self, external_id, text):
        return self._publish_container(self._container(text=text, reply_to=external_id))

    def delete(self, external_id):
        self.http("DELETE", f"{API}/{external_id}", params={"access_token": self.creds["access_token"]})

    # --- metrics ----------------------------------------------------------------------
    def fetch_metrics(self, external_ids):
        out = {}
        for mid in external_ids:
            try:
                data = self._g("GET", f"{mid}/insights", metric="views,likes,replies,reposts,quotes,shares")
            except ProviderError as exc:
                if exc.status in (400, 404):
                    continue
                raise
            v = {
                d["name"]: (d.get("values") or [{}])[0].get("value", (d.get("total_value") or {}).get("value"))
                for d in data.get("data", [])
            }
            out[mid] = Metrics(
                impressions=v.get("views"),
                likes=v.get("likes"),
                comments=v.get("replies"),
                shares=(v.get("reposts") or 0) + (v.get("quotes") or 0) + (v.get("shares") or 0),
                raw=v,
            )
        return out

    def fetch_audience(self):
        data = self._g("GET", f"{self.channel.external_id}/threads_insights", metric="followers_count")
        for d in data.get("data", []):
            if d.get("name") == "followers_count":
                return (d.get("total_value") or {}).get("value")
        return None

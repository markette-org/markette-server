"""Facebook Pages and Instagram professional accounts via the Meta Graph API.

Both providers share one Facebook Login flow; the account picker offers Pages
for Facebook and the Instagram accounts linked to those Pages for Instagram.
"""

import time
from urllib.parse import urlencode

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
from .text import parse_time

SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    # Inbox: read comments on the Page's posts and answer them.
    "pages_read_user_content",
    "pages_manage_engagement",
    "read_insights",
    "business_management",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_insights",
    "instagram_manage_comments",
]


def graph():
    return f"https://graph.facebook.com/{conf.get('META_GRAPH_VERSION')}"


class MetaBase(BaseProvider):
    @classmethod
    def is_configured(cls):
        return bool(conf.get("META_APP_ID") and conf.get("META_APP_SECRET"))

    def authorization_url(self, request):
        params = {
            "client_id": conf.get("META_APP_ID"),
            "redirect_uri": redirect_uri("facebook"),
            "state": request.state,
            "scope": ",".join(SCOPES),
            "response_type": "code",
        }
        return f"https://www.facebook.com/{conf.get('META_GRAPH_VERSION')}/dialog/oauth?{urlencode(params)}"

    def _user_token(self, code):
        short = self.http(
            "GET",
            f"{graph()}/oauth/access_token",
            params={
                "client_id": conf.get("META_APP_ID"),
                "client_secret": conf.get("META_APP_SECRET"),
                "redirect_uri": redirect_uri("facebook"),
                "code": code,
            },
        ).json()["access_token"]
        long = self.http(
            "GET",
            f"{graph()}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": conf.get("META_APP_ID"),
                "client_secret": conf.get("META_APP_SECRET"),
                "fb_exchange_token": short,
            },
        ).json()
        return long["access_token"]

    def complete_oauth(self, request, params):
        user_token = self._user_token(params["code"])
        pages = []
        url = f"{graph()}/me/accounts"
        query = {
            "access_token": user_token,
            "fields": "id,name,access_token,link,picture{url},followers_count,"
            "instagram_business_account{id,username,name,profile_picture_url,followers_count}",
            "limit": 100,
        }
        while url:
            data = self.http("GET", url, params=query).json()
            pages += data.get("data", [])
            url = (data.get("paging") or {}).get("next")
            query = None
        candidates = []
        for p in pages:
            # Page tokens derived from a long-lived user token do not expire.
            candidates.append(
                Candidate(
                    provider="facebook",
                    external_id=p["id"],
                    name=p["name"],
                    avatar_url=((p.get("picture") or {}).get("data") or {}).get("url", ""),
                    profile_url=p.get("link") or f"https://facebook.com/{p['id']}",
                    credentials={"access_token": p["access_token"]},
                    followers_count=p.get("followers_count"),
                )
            )
            ig = p.get("instagram_business_account")
            if ig:
                candidates.append(
                    Candidate(
                        provider="instagram",
                        external_id=ig["id"],
                        name=ig.get("name") or ig.get("username", ""),
                        handle=f"@{ig.get('username', '')}",
                        avatar_url=ig.get("profile_picture_url", ""),
                        profile_url=f"https://instagram.com/{ig.get('username', '')}",
                        credentials={"access_token": p["access_token"], "page_id": p["id"]},
                        followers_count=ig.get("followers_count"),
                    )
                )
        if not candidates:
            raise ProviderError("No Facebook Pages were shared with Markette. Grant access to at least one Page.")
        return candidates

    @property
    def token(self):
        return self.creds["access_token"]

    def g(self, method, path, **kwargs):
        params = {**kwargs.pop("params", {}), "access_token": self.token}
        return self.http(method, f"{graph()}/{path}", params=params, **kwargs).json()

    def error_from_response(self, resp):
        err = super().error_from_response(resp)
        try:
            e = resp.json().get("error", {})
        except ValueError:
            return err
        code = e.get("code")
        if code == 190:  # invalid / expired token
            err.auth = True
        elif code in (4, 17, 32, 613) or e.get("is_transient"):
            err.retryable = True
            err.retry_after = err.retry_after or 600
        return err


class FacebookProvider(MetaBase):
    key = "facebook"
    label = "Facebook"
    capabilities = Capabilities(
        max_chars=63206,
        supports_edit=True,
        history=True,
        inbox=True,
        inbox_interval=10,
        inbox_reply=True,
        max_media=10,
        supports_first_comment=True,
        first_comment_max=8000,
        metric_names=("impressions", "reach", "likes", "comments", "shares", "clicks"),
    )

    def publish(self, content):
        page = self.channel.external_id
        photos = [m for m in content.media if not m.is_video]
        videos = [m for m in content.media if m.is_video]
        if videos:
            res = self.g("POST", f"{page}/videos", data={"file_url": videos[0].url, "description": content.text})
            vid = res["id"]
            return PublishResult(
                external_id=vid, url=f"https://www.facebook.com/{page}/videos/{vid}", extra={"video": True}
            )
        if len(photos) == 1:
            res = self.g("POST", f"{page}/photos", data={"url": photos[0].url, "caption": content.text})
            post_id = res.get("post_id") or res["id"]
        else:
            data = {"message": content.text}
            if content.link:
                data["link"] = content.link
            for i, m in enumerate(photos):
                ph = self.g("POST", f"{page}/photos", data={"url": m.url, "published": "false"})
                data[f"attached_media[{i}]"] = f'{{"media_fbid":"{ph["id"]}"}}'
            post_id = self.g("POST", f"{page}/feed", data=data)["id"]
        return PublishResult(external_id=post_id, url=f"https://www.facebook.com/{post_id}")

    def delete(self, external_id):
        self.g("DELETE", external_id)

    def update(self, external_id, content, extra):
        if "/videos/" in (extra.get("url") or "") or extra.get("video"):
            self.g("POST", external_id, data={"description": content.text})
        else:
            self.g("POST", external_id, data={"message": content.text})

    def post_first_comment(self, external_id, text):
        return self.g("POST", f"{external_id}/comments", data={"message": text})["id"]

    def fetch_history(self, limit=100):
        out, after = [], None
        while len(out) < limit:
            params = {"fields": "id,message,created_time,permalink_url", "limit": 100}
            if after:
                params["after"] = after
            res = self.g("GET", f"{self.channel.external_id}/posts", params=params)
            for p in res.get("data", []):
                out.append(
                    HistoryEntry(
                        external_id=p["id"],
                        published_at=parse_time(p.get("created_time")),
                        text=p.get("message", ""),
                        url=p.get("permalink_url") or f"https://www.facebook.com/{p['id']}",
                    )
                )
            after = ((res.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not res.get("data") or not (res.get("paging") or {}).get("next"):
                break
        return out[:limit]

    def fetch_inbox(self, state, recent_posts):
        page = self.channel.external_id
        entries = []
        for pid in recent_posts[:15]:
            res = self.g(
                "GET",
                f"{pid}/comments",
                params={
                    "fields": "id,message,from{id,name,picture},created_time,permalink_url,parent{id}",
                    "filter": "stream",
                    "order": "reverse_chronological",
                    "limit": 50,
                },
            )
            for c in res.get("data", []):
                who = c.get("from") or {}
                if who.get("id") == page:
                    continue  # the Page's own answers
                entries.append(
                    InboxEntry(
                        external_id=c["id"],
                        kind="comment",
                        text=c.get("message", ""),
                        url=c.get("permalink_url", ""),
                        author_name=who.get("name", ""),
                        author_avatar=((who.get("picture") or {}).get("data") or {}).get("url", ""),
                        created_at=parse_time(c.get("created_time")),
                        post_external_id=pid,
                    )
                )
        return entries, state

    def reply_inbox(self, entry, text):
        res = self.g("POST", f"{entry.external_id}/comments", data={"message": text})
        return PublishResult(external_id=res["id"], url="")

    def fetch_metrics(self, external_ids):
        out = {}
        for pid in external_ids:
            try:
                base = self.g(
                    "GET",
                    pid,
                    params={
                        "fields": "shares,reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)",
                    },
                )
            except ProviderError as exc:
                if exc.status in (400, 404):
                    continue
                raise
            m = Metrics(
                likes=((base.get("reactions") or {}).get("summary") or {}).get("total_count"),
                comments=((base.get("comments") or {}).get("summary") or {}).get("total_count"),
                shares=(base.get("shares") or {}).get("count", 0),
                raw={"fields": base},
            )
            try:
                ins = self.g(
                    "GET",
                    f"{pid}/insights",
                    params={
                        "metric": "post_impressions,post_impressions_unique,post_clicks",
                    },
                )
                values = {i["name"]: (i.get("values") or [{}])[0].get("value") for i in ins.get("data", [])}
                m.impressions = values.get("post_impressions")
                m.reach = values.get("post_impressions_unique")
                m.clicks = values.get("post_clicks")
                m.raw["insights"] = values
            except ProviderError:
                pass  # Insights need read_insights and a page with enough followers.
            out[pid] = m
        return out

    def fetch_audience(self):
        return self.g("GET", self.channel.external_id, params={"fields": "followers_count"}).get("followers_count")


class InstagramProvider(MetaBase):
    key = "instagram"
    label = "Instagram"
    capabilities = Capabilities(
        max_chars=2200,
        max_media=10,
        requires_media=True,
        supports_first_comment=True,
        first_comment_max=2200,
        history=True,
        inbox=True,
        inbox_interval=10,
        inbox_reply=True,
        metric_names=("impressions", "reach", "likes", "comments", "shares", "saves"),
    )

    def fetch_history(self, limit=100):
        out, after = [], None
        while len(out) < limit:
            params = {"fields": "id,caption,timestamp,permalink,like_count,comments_count", "limit": 50}
            if after:
                params["after"] = after
            res = self.g("GET", f"{self.channel.external_id}/media", params=params)
            for m in res.get("data", []):
                out.append(
                    HistoryEntry(
                        external_id=m["id"],
                        published_at=parse_time(m.get("timestamp")),
                        text=m.get("caption", ""),
                        url=m.get("permalink", ""),
                        metrics=Metrics(likes=m.get("like_count"), comments=m.get("comments_count")),
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
            res = self.g(
                "GET",
                f"{mid}/comments",
                params={"fields": "id,text,username,timestamp,replies{id,text,username,timestamp}", "limit": 50},
            )
            for c in res.get("data", []):
                for item in [c, *((c.get("replies") or {}).get("data") or [])]:
                    if (item.get("username") or "").lower() == own:
                        continue
                    entries.append(
                        InboxEntry(
                            external_id=item["id"],
                            kind="comment",
                            text=item.get("text", ""),
                            author_name=item.get("username", ""),
                            author_handle="@" + item.get("username", ""),
                            author_url=f"https://instagram.com/{item.get('username', '')}",
                            created_at=parse_time(item.get("timestamp")),
                            post_external_id=mid,
                            # Instagram replies only nest one level: answer under the top comment.
                            context={"reply_to": c["id"]},
                        )
                    )
        return entries, state

    def reply_inbox(self, entry, text):
        target = entry.context.get("reply_to") or entry.external_id
        res = self.g("POST", f"{target}/replies", data={"message": text})
        return PublishResult(external_id=res["id"], url="")

    def validate(self, content):
        problems = super().validate(content)
        if content.text.count("#") > 30:
            problems.append("Instagram allows at most 30 hashtags.")
        return problems

    @repeatable
    def _container(self, item, *, carousel_item=False, caption=None, extra=None):
        ig = self.channel.external_id
        data = dict(extra or {})
        if item.is_video:
            data.update(media_type="REELS" if not carousel_item else "VIDEO", video_url=item.url)
        else:
            data["image_url"] = item.url
            if item.alt:
                data["alt_text"] = item.alt[:1000]
        if carousel_item:
            data["is_carousel_item"] = "true"
        if caption is not None:
            data["caption"] = caption
        cid = self.g("POST", f"{ig}/media", data=data)["id"]
        if item.is_video:
            self._wait(cid)
        return cid

    def _wait(self, container_id):
        for _ in range(90):
            st = self.g("GET", container_id, params={"fields": "status_code,status"})
            code = st.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise ProviderError(f"Instagram could not process the media: {st.get('status')}")
            time.sleep(5)
        raise ProviderError("Instagram media processing timed out.", retryable=True)

    def publish(self, content):
        ig = self.channel.external_id
        if len(content.media) == 1:
            creation = self._container(content.media[0], caption=content.text)
        else:
            children = [self._container(m, carousel_item=True) for m in content.media]
            creation = self.g(
                "POST",
                f"{ig}/media",
                data={
                    "media_type": "CAROUSEL",
                    "children": ",".join(children),
                    "caption": content.text,
                },
            )["id"]
            self._wait(creation)
        media_id = self.g("POST", f"{ig}/media_publish", data={"creation_id": creation})["id"]
        permalink = self.g("GET", media_id, params={"fields": "permalink"}).get("permalink", "")
        return PublishResult(external_id=media_id, url=permalink)

    def post_first_comment(self, external_id, text):
        return self.g("POST", f"{external_id}/comments", data={"message": text})["id"]

    def fetch_metrics(self, external_ids):
        out = {}
        for mid in external_ids:
            try:
                base = self.g("GET", mid, params={"fields": "like_count,comments_count,media_product_type"})
            except ProviderError as exc:
                if exc.status in (400, 404):
                    continue
                raise
            m = Metrics(likes=base.get("like_count"), comments=base.get("comments_count"), raw={"fields": base})
            try:
                ins = self.g("GET", f"{mid}/insights", params={"metric": "reach,views,saved,shares,total_interactions"})
                values = {i["name"]: (i.get("values") or [{}])[0].get("value") for i in ins.get("data", [])}
                m.reach = values.get("reach")
                m.impressions = values.get("views")
                m.saves = values.get("saved")
                m.shares = values.get("shares")
                m.raw["insights"] = values
            except ProviderError:
                pass
            out[mid] = m
        return out

    def fetch_audience(self):
        return self.g("GET", self.channel.external_id, params={"fields": "followers_count"}).get("followers_count")

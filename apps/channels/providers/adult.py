"""Adult platforms: Pornhub, YouPorn, RedTube, xHamster, OnlyFans, Fansly.

None of them offers an upload API. Publishing is a *hand-off*: at the scheduled time the
publication waits as "Ready to upload" with everything prepared (video, title, description,
tags), the team is notified, someone uploads it on the site and pastes its address back.
A channel can instead use the optional *upload bot* (a headless browser with the account's
own session, run by the separate "uploader" worker); when it cannot finish, the publication
falls back to the hand-off. The bot never solves CAPTCHAs or works around the sites' checks.

These channels only exist in workspaces where an admin turned on adult content.
Pornhub, YouPorn and RedTube views come from the public Aylo webmaster APIs; xHamster,
OnlyFans and Fansly expose no statistics.
"""

import re
from urllib.parse import parse_qs, urlparse

from .base import BaseProvider, Candidate, Capabilities, CredentialField, Metrics, ProviderError

VIDEO_SITE = dict(
    kind="video",
    adult=True,
    handoff=True,
    max_chars=None,
    max_media=1,
    requires_media=True,
    supports_title=True,
    supports_alt_text=False,
    auth="credentials",
    metric_names=("impressions",),
)
FAN_SITE = dict(
    kind="social",
    adult=True,
    handoff=True,
    max_chars=1000,
    max_media=20,
    auth="credentials",
    has_metrics=False,
    metric_names=(),
)

BOT_FIELD = CredentialField(
    "bot_cookies",
    "Session for the upload bot (optional)",
    type="textarea",
    required=False,
    help="Only to let the upload bot publish for you: sign in on the site in your browser, export its cookies as "
    "JSON (e.g. with the Cookie-Editor extension) and paste them here. Without it, Markette prepares each upload "
    "and you finish it on the site. Automating uploads may break the site's terms.",
)


class AdultProvider(BaseProvider):
    """Hand-off publishing; subclasses set the site's addresses and how to read a post's id."""

    site: str = ""
    upload_url: str = ""
    profile_hint: str = ""

    @classmethod
    def profile_field(cls):
        return CredentialField("profile", "Your profile address", type="url", help=cls.profile_hint)

    @classmethod
    def _fields(cls):
        return [cls.profile_field(), BOT_FIELD]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.credential_fields = cls._fields()

    # --- connection -----------------------------------------------------------------
    def username_from(self, profile):
        path = [
            p for p in urlparse(profile if "://" in profile else f"https://{self.site}/{profile}").path.split("/") if p
        ]
        return path[-1].lstrip("@") if path else ""

    def connect_with_credentials(self, data):
        profile = (data.get("profile") or "").strip()
        name = self.username_from(profile)
        if not name or not re.fullmatch(r"[\w.-]{1,80}", name):
            raise ProviderError(f"Enter your {self.label} profile address, e.g. {self.profile_hint}")
        creds = {}
        cookies = (data.get("bot_cookies") or "").strip()
        if cookies:
            from .uploader import parse_cookies

            creds["bot_cookies"] = parse_cookies(cookies, self.site)
        return [
            Candidate(
                external_id=name,
                name=f"{self.label} · {name}",
                handle=f"@{name}",
                profile_url=profile if "://" in profile else f"https://{self.site}/{name}",
                credentials=creds,
                config={"upload_mode": "bot" if cookies else "handoff"},
            )
        ]

    # --- publishing -----------------------------------------------------------------
    @property
    def uses_bot(self):
        return (self.channel.config or {}).get("upload_mode") == "bot" and bool(self.creds.get("bot_cookies"))

    def validate(self, content):
        problems = super().validate(content)
        if self.capabilities.kind == "video":
            if content.media and not content.media[0].is_video:
                problems.append(f"{self.label} needs a video.")
            if len(content.title) > 100:
                problems.append(f"{self.label} titles are limited to 100 characters.")
        return problems

    def handoff(self, content):
        """What a person needs to upload it by hand."""
        return {
            "site": self.label,
            "upload_url": self.upload_url,
            "title": content.title,
            "description": content.text,
            "tags": list((content.options or {}).get("tags") or content.tags or []),
            "media": [{"url": m.url, "filename": m.filename, "mime": m.mime} for m in content.media],
            "options": {k: v for k, v in (content.options or {}).items() if k in ("price", "audience", "category")},
        }

    def publish(self, content):
        from .uploader import upload

        return upload(self, content)

    def post_id(self, url):
        """The site's id for a published post, from its address (None if it does not look like one)."""
        return None


class PornhubProvider(AdultProvider):
    key = "pornhub"
    label = "Pornhub"
    site = "www.pornhub.com"
    upload_url = "https://www.pornhub.com/upload/videodata"
    profile_hint = "https://www.pornhub.com/model/yourname"
    capabilities = Capabilities(**VIDEO_SITE)

    def post_id(self, url):
        key = parse_qs(urlparse(url).query).get("viewkey")
        return key[0] if key else None

    def fetch_metrics(self, external_ids):
        out = {}
        for vid in external_ids:
            v = self.http("GET", "https://www.pornhub.com/webmasters/video_by_id", params={"id": vid}).json()
            video = v.get("video") or {}
            if video:
                out[vid] = Metrics(impressions=_int(video.get("views")), raw={"rating": video.get("rating")})
        return out


class YouPornProvider(AdultProvider):
    key = "youporn"
    label = "YouPorn"
    site = "www.youporn.com"
    upload_url = "https://www.youporn.com/upload/"
    profile_hint = "https://www.youporn.com/uservids/yourname/"
    capabilities = Capabilities(**VIDEO_SITE)

    def post_id(self, url):
        m = re.search(r"/watch/(\d+)", urlparse(url).path)
        return m.group(1) if m else None

    def fetch_metrics(self, external_ids):
        out = {}
        for vid in external_ids:
            v = self.http("GET", "https://www.youporn.com/api/webmasters/video_by_id/", params={"video_id": vid}).json()
            video = (v.get("video") or {}).get("video", v.get("video") or {})
            if video:
                out[vid] = Metrics(impressions=_int(video.get("views")), raw={"rating": video.get("rating")})
        return out


class RedTubeProvider(AdultProvider):
    key = "redtube"
    label = "RedTube"
    site = "www.redtube.com"
    upload_url = "https://www.redtube.com/upload"
    profile_hint = "https://www.redtube.com/users/yourname"
    capabilities = Capabilities(**VIDEO_SITE)

    def post_id(self, url):
        m = re.fullmatch(r"/(\d+)/?", urlparse(url).path)
        return m.group(1) if m else None

    def fetch_metrics(self, external_ids):
        out = {}
        for vid in external_ids:
            v = self.http(
                "GET",
                "https://api.redtube.com/",
                params={"data": "redtube.Videos.getVideoById", "video_id": vid, "output": "json"},
            ).json()
            video = (v.get("video") or {}) if isinstance(v, dict) else {}
            if video:
                out[vid] = Metrics(impressions=_int(video.get("views")), raw={"rating": video.get("rating")})
        return out


class XHamsterProvider(AdultProvider):
    key = "xhamster"
    label = "xHamster"
    site = "xhamster.com"
    upload_url = "https://xhamster.com/upload"
    profile_hint = "https://xhamster.com/users/yourname"
    capabilities = Capabilities(**{**VIDEO_SITE, "has_metrics": False, "metric_names": ()})

    def post_id(self, url):
        m = re.search(r"/videos/([\w-]+)", urlparse(url).path)
        return m.group(1) if m else None


class OnlyFansProvider(AdultProvider):
    key = "onlyfans"
    label = "OnlyFans"
    site = "onlyfans.com"
    upload_url = "https://onlyfans.com/posts/create"
    profile_hint = "https://onlyfans.com/yourname"
    capabilities = Capabilities(**FAN_SITE)

    def post_id(self, url):
        m = re.match(r"/(\d+)/[\w.-]+", urlparse(url).path)
        return m.group(1) if m else None


class FanslyProvider(AdultProvider):
    key = "fansly"
    label = "Fansly"
    site = "fansly.com"
    upload_url = "https://fansly.com/home"
    profile_hint = "https://fansly.com/yourname"
    capabilities = Capabilities(**FAN_SITE)

    def post_id(self, url):
        m = re.search(r"/post/(\d+)", urlparse(url).path)
        return m.group(1) if m else None


def _int(value):
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None

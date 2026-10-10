"""Provider contract shared by every network and publishing backend."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
from django.conf import settings

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """A failed call to a provider.

    ``retryable`` errors (rate limits, 5xx, network) are retried with backoff;
    ``auth`` errors flag the channel for re-authentication; anything else is a
    permanent failure of that one publication.
    """

    def __init__(self, message, *, retryable=False, auth=False, retry_after=None, status=None, payload=None):
        super().__init__(message)
        self.retryable = retryable
        self.auth = auth
        self.retry_after = retry_after
        self.status = status
        self.payload = payload


class NotConfigured(ProviderError):
    pass


@dataclass
class Capabilities:
    kind: str = "social"  # "social" or "blog"
    max_chars: int | None = None
    count_mode: str = "chars"  # chars | graphemes | x (URLs weigh 23)
    max_media: int = 4
    requires_media: bool = False
    supports_video: bool = True
    supports_title: bool = False
    supports_alt_text: bool = True
    has_metrics: bool = True
    metric_names: tuple[str, ...] = ("impressions", "likes", "comments", "shares")
    auth: str = "oauth2"  # oauth2 | credentials
    # Can post a comment under the published post (e.g. links in LinkedIn's first comment).
    supports_first_comment: bool = False
    first_comment_max: int | None = None
    instance_url: bool = False  # user must provide a server/site URL


@dataclass
class MediaItem:
    path: Path
    url: str  # publicly reachable URL (Instagram/Facebook fetch media themselves)
    mime: str
    alt: str = ""
    width: int | None = None
    height: int | None = None
    size: int = 0
    filename: str = ""

    @property
    def is_video(self):
        return self.mime.startswith("video/")

    def read(self) -> bytes:
        return self.path.read_bytes()


@dataclass
class Content:
    """What a single channel should publish, already resolved per channel."""

    text: str
    title: str = ""
    link: str = ""
    media: list[MediaItem] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class PublishResult:
    external_id: str
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Metrics:
    impressions: int | None = None
    reach: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    clicks: int | None = None
    video_views: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    """An account found during connection that the user may add as a channel."""

    external_id: str
    name: str
    handle: str = ""
    avatar_url: str = ""
    profile_url: str = ""
    base_url: str = ""
    credentials: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    token_expires_at: datetime | None = None
    followers_count: int | None = None
    provider: str = ""  # set when one auth flow yields channels of another provider


@dataclass
class CredentialField:
    name: str
    label: str
    type: str = "text"  # text | password | url | textarea | select | checkbox
    required: bool = True
    help: str = ""
    default: Any = None
    choices: list[str] | None = None


def pkce_pair():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def redirect_uri(provider_key):
    return f"{settings.PUBLIC_API_URL}/api/oauth/{provider_key}/callback"


class BaseProvider:
    key: ClassVar[str]
    label: ClassVar[str]
    capabilities: ClassVar[Capabilities] = Capabilities()
    credential_fields: ClassVar[list[CredentialField]] = []
    timeout = httpx.Timeout(30.0, connect=10.0)

    def __init__(self, channel=None):
        self.channel = channel

    # --- configuration ---------------------------------------------------
    @classmethod
    def is_configured(cls) -> bool:
        return True

    @classmethod
    def describe(cls):
        caps = cls.capabilities
        return {
            "key": cls.key,
            "label": cls.label,
            "configured": cls.is_configured(),
            "kind": caps.kind,
            "auth": caps.auth,
            "instance_url": caps.instance_url,
            "max_chars": caps.max_chars,
            "count_mode": caps.count_mode,
            "max_media": caps.max_media,
            "requires_media": caps.requires_media,
            "supports_video": caps.supports_video,
            "supports_title": caps.supports_title,
            "supports_alt_text": caps.supports_alt_text,
            "has_metrics": caps.has_metrics,
            "metric_names": list(caps.metric_names),
            "credential_fields": [f.__dict__ for f in cls.credential_fields],
            "supports_first_comment": caps.supports_first_comment,
            "first_comment_max": caps.first_comment_max,
        }

    # --- connection ------------------------------------------------------
    def authorization_url(self, request) -> str:
        raise NotImplementedError

    def complete_oauth(self, request, params) -> list[Candidate]:
        raise NotImplementedError

    def connect_with_credentials(self, data) -> list[Candidate]:
        raise NotImplementedError

    def refresh_credentials(self) -> bool:
        """Refresh tokens if needed. Returns True when credentials changed."""
        return False

    # --- content ---------------------------------------------------------
    def validate(self, content: Content) -> list[str]:
        from .text import measure

        caps = self.capabilities
        problems = []
        comment = ""
        posted_text = content.text
        if caps.supports_first_comment:
            from copy import deepcopy

            post, comment = self.prepare_first_comment(deepcopy(content))
            posted_text = post.text  # links may move to the comment
        if caps.max_chars and measure(posted_text, caps.count_mode) > caps.max_chars:
            problems.append(f"Text exceeds {caps.max_chars} characters.")
        if caps.first_comment_max and len(comment) > caps.first_comment_max:
            problems.append(f"The first comment exceeds {caps.first_comment_max} characters.")
        if caps.requires_media and not content.media:
            problems.append(f"{self.label} requires at least one image or video.")
        if len(content.media) > caps.max_media:
            problems.append(f"{self.label} accepts at most {caps.max_media} media items.")
        if not caps.supports_video and any(m.is_video for m in content.media):
            problems.append(f"{self.label} does not support video here.")
        if caps.supports_title and not content.title:
            problems.append("A title is required.")
        if not content.text.strip() and not content.media:
            problems.append("Nothing to publish.")
        return problems

    # --- first comment -----------------------------------------------------
    def prepare_first_comment(self, content: Content) -> tuple[Content, str]:
        """Split what goes in the post from what goes in its first comment.

        ``first_comment`` (target option) is posted as is. With ``links_in_first_comment``
        (target option, defaulting to the channel's setting) every URL is taken out of the
        post text, the link card is dropped, and the URLs are appended to the comment, since
        some networks (LinkedIn above all) show posts with links to fewer people.
        """
        from .text import URL_RE

        if not self.capabilities.supports_first_comment:
            return content, ""
        opts = content.options
        comment = (opts.get("first_comment") or "").strip()
        move = opts.get("links_in_first_comment")
        if move is None and self.channel is not None:
            move = self.channel.config.get("links_in_first_comment", False)
        if move:
            urls = [u.rstrip(".,;:!?)") for u in URL_RE.findall(content.text)]
            if content.link:
                urls.append(content.link)
            urls = list(dict.fromkeys(urls))
            if urls:
                text = content.text
                for u in urls:
                    text = text.replace(u, "")
                # Drop punctuation left standing alone where a link was ("see https://x." → "see").
                text = re.sub(r"[ \t]+[.,;!?]+(?=\s|$)", "", text)
                lines = [" ".join(line.split()) for line in text.splitlines()]
                content.text = "\n".join(lines).strip()
                while "\n\n\n" in content.text:
                    content.text = content.text.replace("\n\n\n", "\n\n")
                content.link = ""
                comment = "\n".join(filter(None, [comment, *urls]))
        return content, comment

    def post_first_comment(self, external_id: str, text: str) -> str:
        """Comment under a published post; returns the comment id."""
        raise ProviderError(f"{self.label} does not support first comments.")

    def publish(self, content: Content) -> PublishResult:
        raise NotImplementedError

    def fetch_metrics(self, external_ids: list[str]) -> dict[str, Metrics]:
        return {}

    def fetch_audience(self) -> int | None:
        return None

    def delete(self, external_id: str) -> None:
        raise ProviderError(f"{self.label} does not support deleting from Markette.")

    # --- helpers ---------------------------------------------------------
    @property
    def creds(self):
        return self.channel.credentials if self.channel else {}

    def save_credentials(self, creds, expires_at=None):
        self.channel.credentials = creds
        fields = ["credentials", "updated_at"]
        if expires_at is not None:
            self.channel.token_expires_at = expires_at
            fields.append("token_expires_at")
        self.channel.save(update_fields=fields)

    def http(self, method, url, *, expect=(200, 201, 202, 204), **kwargs) -> httpx.Response:
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                resp = client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.label}: timeout calling {url}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.label}: network error: {exc}", retryable=True) from exc
        if resp.status_code in expect:
            return resp
        raise self.error_from_response(resp)

    def error_from_response(self, resp: httpx.Response) -> ProviderError:
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text[:2000]
        msg = f"{self.label} API error {resp.status_code}: {_short(payload)}"
        status = resp.status_code
        if status == 429:
            retry_after = resp.headers.get("retry-after") or resp.headers.get("x-rate-limit-reset")
            return ProviderError(msg, retryable=True, retry_after=_seconds(retry_after), status=status, payload=payload)
        if status in (401,):
            return ProviderError(msg, auth=True, status=status, payload=payload)
        if status >= 500 or status in (408, 409):
            return ProviderError(msg, retryable=True, status=status, payload=payload)
        return ProviderError(msg, status=status, payload=payload)


def _short(payload, limit=500):
    s = str(payload)
    return s if len(s) <= limit else s[:limit] + "…"


def _seconds(value):
    if not value:
        return None
    try:
        v = int(float(value))
    except ValueError:
        return None
    # X sends an epoch timestamp in x-rate-limit-reset.
    if v > 10**9:
        import time

        v = max(int(v - time.time()), 1)
    return v

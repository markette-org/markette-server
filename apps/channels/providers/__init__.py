from .base import (
    BaseProvider,
    Candidate,
    Capabilities,
    Content,
    InboxEntry,
    MediaItem,
    Metrics,
    ProviderError,
    PublishResult,
)
from .blogs import DrupalProvider, WordPressProvider
from .bluesky import BlueskyProvider
from .devto import DevtoProvider
from .discord import DiscordProvider
from .hugo import HugoProvider
from .linkedin import LinkedInProvider
from .mastodon import MastodonProvider
from .medium import MediumProvider
from .meta import FacebookProvider, InstagramProvider
from .newsletters import BrevoProvider, GhostProvider, ListmonkProvider
from .qq import QQProvider
from .reddit import RedditProvider
from .slack import SlackProvider
from .telegram import TelegramProvider
from .threads import ThreadsProvider
from .webhook import WebhookProvider
from .wechat import WeChatProvider
from .whatsapp import WhatsAppProvider
from .x import XProvider
from .youtube import YouTubeProvider

REGISTRY: dict[str, type[BaseProvider]] = {
    p.key: p
    for p in (
        XProvider,
        BlueskyProvider,
        ThreadsProvider,
        RedditProvider,
        MastodonProvider,
        LinkedInProvider,
        FacebookProvider,
        InstagramProvider,
        YouTubeProvider,
        TelegramProvider,
        WhatsAppProvider,
        WeChatProvider,
        QQProvider,
        DiscordProvider,
        SlackProvider,
        WordPressProvider,
        DrupalProvider,
        MediumProvider,
        DevtoProvider,
        GhostProvider,
        HugoProvider,
        BrevoProvider,
        ListmonkProvider,
        WebhookProvider,
    )
}


def get_provider(key) -> type[BaseProvider]:
    try:
        return REGISTRY[key]
    except KeyError as exc:
        raise ProviderError(f"Unknown provider: {key}") from exc


__all__ = [
    "REGISTRY",
    "get_provider",
    "BaseProvider",
    "Candidate",
    "Capabilities",
    "Content",
    "InboxEntry",
    "MediaItem",
    "Metrics",
    "ProviderError",
    "PublishResult",
]

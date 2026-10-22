import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.fields import EncryptedJSONField
from apps.core.models import TimeStamped
from apps.tenants.models import Organization, Workspace


class Provider(models.TextChoices):
    X = "x", "X"
    BLUESKY = "bluesky", "Bluesky"
    MASTODON = "mastodon", "Mastodon"
    LINKEDIN = "linkedin", "LinkedIn"
    FACEBOOK = "facebook", "Facebook"
    INSTAGRAM = "instagram", "Instagram"
    TELEGRAM = "telegram", "Telegram"
    WHATSAPP = "whatsapp", "WhatsApp"
    WECHAT = "wechat", "WeChat"
    QQ = "qq", "QQ"
    THREADS = "threads", "Threads"
    YOUTUBE = "youtube", "YouTube"
    REDDIT = "reddit", "Reddit"
    DISCORD = "discord", "Discord"
    SLACK = "slack", "Slack"
    GHOST = "ghost", "Ghost"
    BREVO = "brevo", "Brevo"
    LISTMONK = "listmonk", "Listmonk"
    WEBHOOK = "webhook", "Webhook"
    WORDPRESS = "wordpress", "WordPress"
    DRUPAL = "drupal", "Drupal"
    MEDIUM = "medium", "Medium"
    DEVTO = "devto", "DEV"
    HUGO = "hugo", "Hugo (git)"
    TELEGRAM_USER = "telegram_user", "Telegram (personal account)"
    PORNHUB = "pornhub", "Pornhub"
    YOUPORN = "youporn", "YouPorn"
    REDTUBE = "redtube", "RedTube"
    XHAMSTER = "xhamster", "xHamster"
    ONLYFANS = "onlyfans", "OnlyFans"
    FANSLY = "fansly", "Fansly"
    FANVUE = "fanvue", "Fanvue"
    XVIDEOS = "xvideos", "XVideos"
    XNXX = "xnxx", "XNXX"
    SPANKBANG = "spankbang", "SpankBang"
    EPORNER = "eporner", "Eporner"
    MANYVIDS = "manyvids", "ManyVids"
    CLIPS4SALE = "clips4sale", "Clips4Sale"


class ChannelStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    NEEDS_REAUTH = "needs_reauth", "Needs re-authentication"
    DISABLED = "disabled", "Disabled"


class Channel(TimeStamped):
    """A publishing destination: a social profile/page, a blog or a static site.

    Channels belong to the organization and are available in all of its workspaces.
    ``workspaces`` lists the workspaces for which the channel is *relevant*: it is shown
    first and highlighted there (e.g. the brand's own accounts), the others are listed
    under "other channels in your organization".
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="channels")
    workspaces = models.ManyToManyField(Workspace, through="ChannelAccess", related_name="channels", blank=True)
    provider = models.CharField(max_length=20, choices=Provider.choices)
    external_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    handle = models.CharField(max_length=255, blank=True)
    avatar_url = models.URLField(max_length=1000, blank=True)
    profile_url = models.URLField(max_length=1000, blank=True)
    # Instance / site base URL for federated or self-hosted providers.
    base_url = models.URLField(max_length=500, blank=True)
    credentials = EncryptedJSONField(default=dict, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    # Non-secret per-channel configuration (e.g. Drupal bundle, Hugo branch).
    config = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=ChannelStatus.choices, default=ChannelStatus.ACTIVE)
    last_error = models.TextField(blank=True)
    followers_count = models.PositiveIntegerField(null=True, blank=True)
    color = models.CharField(max_length=9, blank=True)
    connected_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "provider", "external_id"], name="uniq_org_channel"),
        ]
        ordering = ["provider", "name"]

    def __str__(self):
        return f"{self.get_provider_display()}: {self.name}"

    def get_provider(self):
        from .providers import get_provider

        return get_provider(self.provider)(channel=self)

    @classmethod
    def create_for(cls, workspace, **fields):
        """Create a channel in the workspace's organization, marked relevant for that workspace."""
        channel = cls.objects.create(organization=workspace.organization, **fields)
        ChannelAccess.objects.create(channel=channel, workspace=workspace)
        return channel

    @property
    def tz(self):
        """Time zone for best-time buckets: the first workspace it was shared with, else UTC."""
        import zoneinfo

        access = self.access.select_related("workspace").order_by("created_at").first()
        return access.workspace.tz if access else zoneinfo.ZoneInfo("UTC")

    def mark_needs_reauth(self, message):
        newly = self.status != ChannelStatus.NEEDS_REAUTH
        self.status = ChannelStatus.NEEDS_REAUTH
        self.last_error = message[:2000]
        self.save(update_fields=["status", "last_error", "updated_at"])
        if newly:
            from apps.notifications.events import emit_for_channel
            from apps.notifications.models import Event

            emit_for_channel(self, Event.RECONNECT)


class ChannelAccess(models.Model):
    """Marks an organization channel as relevant for a workspace."""

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="access")
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="channel_access")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["channel", "workspace"], name="uniq_channel_access")]


class OAuthApp(TimeStamped):
    """Dynamically registered OAuth client (Mastodon registers one app per instance)."""

    provider = models.CharField(max_length=20, choices=Provider.choices)
    base_url = models.URLField(max_length=500)
    client_id = models.CharField(max_length=255)
    secrets = EncryptedJSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "base_url"], name="uniq_oauth_app")]


def _state():
    return secrets.token_urlsafe(32)


def _expiry():
    return timezone.now() + timedelta(minutes=30)


class ConnectionRequest(TimeStamped):
    """One in-flight attempt to connect channels: OAuth state plus the accounts found."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="connection_requests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    provider = models.CharField(max_length=20, choices=Provider.choices)
    state = models.CharField(max_length=64, unique=True, default=_state)
    base_url = models.URLField(max_length=500, blank=True)
    # PKCE verifier and anything else the provider needs between redirect and callback.
    secrets = EncryptedJSONField(default=dict)
    # Accounts discovered after authorization, waiting for the user to pick.
    candidates = EncryptedJSONField(default=dict)
    error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(default=_expiry)

    @property
    def expired(self):
        return timezone.now() > self.expires_at


class Subscriber(TimeStamped):
    """Someone who opted in to receive a channel's broadcasts (WhatsApp)."""

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="subscribers")
    address = models.CharField(max_length=64)  # E.164 phone number for WhatsApp
    name = models.CharField(max_length=200, blank=True)
    # How consent was given: "keyword" (they messaged JOIN), "import", "manual".
    source = models.CharField(max_length=20, default="manual")
    opted_in_at = models.DateTimeField()
    opted_out_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["channel", "address"], name="uniq_subscriber")]
        ordering = ["-opted_in_at"]

    @property
    def active(self):
        return self.opted_out_at is None


class Delivery(models.Model):
    """One broadcast message to one subscriber, with its delivery status from webhooks."""

    target = models.ForeignKey("publishing.PostTarget", on_delete=models.CASCADE, related_name="deliveries")
    subscriber = models.ForeignKey(Subscriber, null=True, on_delete=models.SET_NULL)
    message_id = models.CharField(max_length=128, db_index=True)
    status = models.CharField(max_length=16, default="sent")  # sent, delivered, read, failed
    error = models.CharField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class InboundChat(models.Model):
    """Chats a bot has been added to, learned from webhooks (QQ groups)."""

    provider = models.CharField(max_length=20)
    app_id = models.CharField(max_length=64)
    chat_id = models.CharField(max_length=128)
    title = models.CharField(max_length=200, blank=True)
    seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "app_id", "chat_id"], name="uniq_inbound_chat")]

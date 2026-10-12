"""Where a workspace hears about what happens: email, a Telegram status bot, signed webhooks."""

import secrets
import uuid

from django.db import models

from apps.core.fields import EncryptedJSONField
from apps.core.models import TimeStamped
from apps.tenants.models import Workspace


class Event(models.TextChoices):
    PUBLISHED = "publication.published", "A publication went live"
    POST_PUBLISHED = "post.published", "Every channel of a post is live"
    FAILED = "publication.failed", "A publication failed"
    UNCONFIRMED = "publication.unconfirmed", "A publication needs checking"
    RECONNECT = "channel.needs_reconnect", "A channel needs to be reconnected"
    APPROVAL_REQUESTED = "approval.requested", "A post waits for approval"
    APPROVED = "approval.approved", "A post was approved"
    CHANGES_REQUESTED = "approval.changes_requested", "Changes were requested on a post"
    DIGEST = "digest.daily", "Daily summary (8:00 in the workspace's time zone)"


# What a new destination listens to unless told otherwise: the things someone has to act on.
DEFAULT_EVENTS = [Event.FAILED, Event.UNCONFIRMED, Event.RECONNECT, Event.APPROVAL_REQUESTED]


class Kind(models.TextChoices):
    EMAIL = "email", "Email"
    TELEGRAM = "telegram", "Telegram bot"
    WEBHOOK = "webhook", "Webhook"


# Consecutive failed deliveries after which a destination is switched off.
DISABLE_AFTER = 20


def new_signing_secret():
    return "whsec_" + secrets.token_urlsafe(32)


class Destination(TimeStamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="notification_destinations")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    name = models.CharField(max_length=120)
    events = models.JSONField(default=list)
    enabled = models.BooleanField(default=True)
    # email: {"to": [...]} (empty = the workspace's admins); telegram: {"chat_id", "chat_title"}; webhook: {"url"}
    config = models.JSONField(default=dict, blank=True)
    # telegram: {"bot_token"}; webhook: {"signing_secret"}. Write-only in the API.
    secrets = EncryptedJSONField(default=dict, blank=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    failures = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.name}"


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class Delivery(models.Model):
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    destination = models.ForeignKey(Destination, on_delete=models.CASCADE, related_name="deliveries")
    event = models.CharField(max_length=40)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]

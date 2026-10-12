"""Replies, mentions and comments collected from the networks, answered from Markette."""

from django.conf import settings
from django.db import models

from apps.channels.models import Channel
from apps.publishing.models import PostTarget


class ItemStatus(models.TextChoices):
    NEW = "new", "New"
    READ = "read", "Read"
    DONE = "done", "Done"


class InboxItem(models.Model):
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="inbox_items")
    external_id = models.CharField(max_length=500)
    kind = models.CharField(max_length=16)  # reply | mention | comment | quote
    text = models.TextField(blank=True)
    url = models.URLField(max_length=1000, blank=True)
    author_name = models.CharField(max_length=200, blank=True)
    author_handle = models.CharField(max_length=200, blank=True)
    author_avatar = models.URLField(max_length=1000, blank=True)
    author_url = models.URLField(max_length=1000, blank=True)
    remote_created_at = models.DateTimeField(null=True, blank=True)
    post_external_id = models.CharField(max_length=500, blank=True)
    # Our publication it answers, when it answers one.
    target = models.ForeignKey(PostTarget, null=True, blank=True, on_delete=models.SET_NULL, related_name="inbox_items")
    context = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=8, choices=ItemStatus.choices, default=ItemStatus.NEW)
    reply_text = models.TextField(blank=True)
    reply_url = models.URLField(max_length=1000, blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    replied_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-remote_created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["channel", "external_id"], name="uniq_inbox_item")]
        indexes = [models.Index(fields=["channel", "status"])]


class InboxState(models.Model):
    """Where polling a channel's inbox left off."""

    channel = models.OneToOneField(Channel, on_delete=models.CASCADE, related_name="inbox_state")
    state = models.JSONField(default=dict, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    next_poll_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

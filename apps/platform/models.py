from django.conf import settings
from django.db import models

from apps.core.fields import EncryptedJSONField


class PlatformSettings(models.Model):
    """Installation-wide settings edited by superadmins. A single row (pk=1).

    ``overrides`` and ``secrets`` are keyed by Django setting names (see conf.py);
    a missing or empty value falls back to the environment.
    """

    instance_name = models.CharField(max_length=80, default="Markette")
    support_email = models.EmailField(blank=True)
    announcement = models.TextField(blank=True)
    announcement_level = models.CharField(
        max_length=10, default="info", choices=[("info", "Info"), ("warning", "Warning"), ("critical", "Critical")]
    )
    signups_open = models.BooleanField(default=True)
    # Whether signed-in users may create additional organizations themselves.
    org_creation_open = models.BooleanField(default=True)
    # Comma-separated email domains allowed to self-register; empty = any.
    signup_domains = models.TextField(blank=True)
    # Emergency stop: the scheduler hands nothing to workers while set.
    publishing_paused = models.BooleanField(default=False)
    # Limits given to organizations created by sign-up or without explicit limits; null = unlimited.
    default_max_workspaces = models.PositiveIntegerField(null=True, blank=True)
    default_max_channels = models.PositiveIntegerField(null=True, blank=True)
    default_max_members = models.PositiveIntegerField(null=True, blank=True)
    default_ai_monthly_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    overrides = models.JSONField(default=dict, blank=True)
    secrets = EncryptedJSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        verbose_name = verbose_name_plural = "platform settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        from . import conf

        conf.invalidate()

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def default_limits(self):
        return {
            "max_workspaces": self.default_max_workspaces,
            "max_channels": self.default_max_channels,
            "max_members": self.default_max_members,
            "ai_monthly_tokens": self.default_ai_monthly_tokens,
        }

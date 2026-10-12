"""Short links that count clicks per publication."""

import secrets
import string

from django.db import models

from apps.publishing.models import PostTarget
from apps.tenants.models import Workspace

ALPHABET = string.ascii_letters + string.digits


def new_code():
    return "".join(secrets.choice(ALPHABET) for _ in range(7))


class ShortLink(models.Model):
    code = models.CharField(max_length=16, unique=True, default=new_code)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="short_links")
    target = models.ForeignKey(PostTarget, on_delete=models.CASCADE, related_name="short_links", null=True)
    url = models.URLField(max_length=2000)
    clicks = models.PositiveIntegerField(default=0)
    last_clicked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["target", "url"], name="uniq_short_link_per_target")]


class LinkClickDay(models.Model):
    link = models.ForeignKey(ShortLink, on_delete=models.CASCADE, related_name="days")
    day = models.DateField()
    clicks = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["day"]
        constraints = [models.UniqueConstraint(fields=["link", "day"], name="uniq_link_click_day")]

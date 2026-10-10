from django.conf import settings
from django.db import models

from apps.channels.models import Channel
from apps.tenants.models import Workspace


class BestTimeProfile(models.Model):
    channel = models.OneToOneField(Channel, on_delete=models.CASCADE, related_name="best_time")
    scores = models.JSONField()  # 7×24, Monday first, local hours, 0–100
    counts = models.JSONField(default=list)  # posts observed per bucket
    sample_size = models.PositiveIntegerField(default=0)
    computed_at = models.DateTimeField()


class AIRequest(models.Model):
    """Audit log of AI calls: what was asked, what came back, what it cost."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="ai_requests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=40)
    model = models.CharField(max_length=60)
    prompt = models.TextField()
    response = models.JSONField(default=dict)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    # Cost reported by the provider (OpenRouter); null means estimate from AI_PRICE_* settings.
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    stop_reason = models.CharField(max_length=40, blank=True)
    request_id = models.CharField(max_length=100, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

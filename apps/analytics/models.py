from django.db import models

from apps.channels.models import Channel
from apps.publishing.models import PostTarget

METRIC_FIELDS = ("impressions", "reach", "likes", "comments", "shares", "saves", "clicks", "video_views")
ENGAGEMENT_FIELDS = ("likes", "comments", "shares", "saves", "clicks")


class MetricSnapshot(models.Model):
    """Metrics of one publication at one moment. A series per target shows how a post grew."""

    target = models.ForeignKey(PostTarget, on_delete=models.CASCADE, related_name="snapshots")
    captured_at = models.DateTimeField(db_index=True)
    age_hours = models.FloatField()
    impressions = models.PositiveBigIntegerField(null=True)
    reach = models.PositiveBigIntegerField(null=True)
    likes = models.PositiveIntegerField(null=True)
    comments = models.PositiveIntegerField(null=True)
    shares = models.PositiveIntegerField(null=True)
    saves = models.PositiveIntegerField(null=True)
    clicks = models.PositiveIntegerField(null=True)
    video_views = models.PositiveBigIntegerField(null=True)
    engagements = models.PositiveIntegerField(default=0)
    # engagements / impressions when known, else engagements / followers.
    engagement_rate = models.FloatField(null=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["captured_at"]
        indexes = [models.Index(fields=["target", "captured_at"], name="snapshot_target_idx")]

    def as_dict(self):
        d = {f: getattr(self, f) for f in METRIC_FIELDS}
        d.update(
            engagements=self.engagements,
            engagement_rate=self.engagement_rate,
            captured_at=self.captured_at.isoformat(),
            age_hours=round(self.age_hours, 1),
        )
        return d


class AudienceSnapshot(models.Model):
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="audience")
    date = models.DateField()
    followers = models.PositiveIntegerField()

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["channel", "date"], name="uniq_audience_day")]


class ReportPeriod(models.TextChoices):
    LAST_7 = "7d", "Last 7 days"
    LAST_30 = "30d", "Last 30 days"
    PREVIOUS_WEEK = "previous_week", "Previous week (Monday–Sunday)"
    PREVIOUS_MONTH = "previous_month", "Previous month"


class ReportFrequency(models.TextChoices):
    NEVER = "", "Not emailed"
    WEEKLY = "weekly", "Every Monday"
    MONTHLY = "monthly", "On the 1st of the month"


class Report(models.Model):
    """A saved analytics report: emailed on a schedule and/or shared as a read-only page."""

    workspace = models.ForeignKey("tenants.Workspace", on_delete=models.CASCADE, related_name="reports")
    name = models.CharField(max_length=120)
    period = models.CharField(max_length=20, choices=ReportPeriod.choices, default=ReportPeriod.LAST_30)
    frequency = models.CharField(max_length=10, choices=ReportFrequency.choices, blank=True, default="")
    recipients = models.JSONField(default=list, blank=True)
    campaign = models.ForeignKey("publishing.Campaign", null=True, blank=True, on_delete=models.SET_NULL)
    channels = models.JSONField(default=list, blank=True)  # channel ids; empty = all
    # Anyone with the link sees the report (read-only). Null = not shared.
    share_token = models.CharField(max_length=64, unique=True, null=True, blank=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_by = models.ForeignKey("core.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "pk"]


class HistoryImport(models.Model):
    """Bringing a channel's past posts into a workspace, for analytics and best times."""

    channel = models.ForeignKey("channels.Channel", on_delete=models.CASCADE, related_name="history_imports")
    workspace = models.ForeignKey("tenants.Workspace", on_delete=models.CASCADE, related_name="history_imports")
    requested_by = models.ForeignKey("core.User", null=True, on_delete=models.SET_NULL)
    limit = models.PositiveIntegerField(default=100)
    status = models.CharField(max_length=10, default="queued")  # queued | running | done | failed
    found = models.PositiveIntegerField(default=0)
    imported = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

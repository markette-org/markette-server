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

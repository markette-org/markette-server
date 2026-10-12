import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.channels.models import Channel
from apps.core.models import TimeStamped
from apps.tenants.models import Workspace


def media_path(instance, filename):
    ext = Path(filename).suffix.lower()[:10]
    return f"ws-{instance.workspace_id}/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{ext}"


class MediaAsset(TimeStamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="media")
    file = models.FileField(upload_to=media_path, max_length=300)
    original_name = models.CharField(max_length=255, blank=True)
    mime = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    alt_text = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at"]

    @property
    def public_url(self):
        return f"{settings.PUBLIC_API_URL}{settings.MEDIA_URL}{self.file.name}"

    @property
    def is_video(self):
        return self.mime.startswith("video/")


class Campaign(TimeStamped):
    """A period with a theme. Posts can belong to it; the AI plans within it."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="campaigns")
    name = models.CharField(max_length=200)
    brief = models.TextField(blank=True)
    starts_on = models.DateField()
    ends_on = models.DateField()
    color = models.CharField(max_length=9, default="#6d5efc")

    class Meta:
        ordering = ["-starts_on"]


class PostStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SCHEDULED = "scheduled", "Scheduled"
    PUBLISHING = "publishing", "Publishing"
    PUBLISHED = "published", "Published"
    PARTIAL = "partial", "Partially published"
    UNCONFIRMED = "unconfirmed", "Needs checking"
    FAILED = "failed", "Failed"


class ScheduleMode(models.TextChoices):
    EXACT = "exact", "At a specific time"
    QUEUE = "queue", "Next queue slot"
    BEST_TIME = "best_time", "Best time"
    NOW = "now", "Now"


class ApprovalStatus(models.TextChoices):
    NONE = "", "Not required"
    PENDING = "pending", "Waiting for review"
    APPROVED = "approved", "Approved"
    CHANGES_REQUESTED = "changes_requested", "Changes requested"


class Post(TimeStamped):
    """The editorial unit. It fans out to one PostTarget per channel."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="posts")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="posts")
    campaign = models.ForeignKey(Campaign, null=True, blank=True, on_delete=models.SET_NULL, related_name="posts")
    title = models.CharField(max_length=300, blank=True)
    body = models.TextField(blank=True)
    # Brought in from a network's history (published outside Markette); read-only for analytics.
    imported = models.BooleanField(default=False)
    link = models.URLField(max_length=1000, blank=True)
    tags = models.JSONField(default=list, blank=True)
    media = models.ManyToManyField(MediaAsset, through="PostMedia", blank=True, related_name="posts")
    status = models.CharField(max_length=16, choices=PostStatus.choices, default=PostStatus.DRAFT, db_index=True)
    schedule_mode = models.CharField(max_length=16, choices=ScheduleMode.choices, default=ScheduleMode.EXACT)
    # Window used by queue / best-time scheduling.
    window_start = models.DateTimeField(null=True, blank=True)
    window_end = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    # Team approval workflow (see apps.teams.access).
    approval_status = models.CharField(
        max_length=20, choices=ApprovalStatus.choices, blank=True, default="", db_index=True
    )
    # The scheduling request made by the author, applied when a reviewer approves.
    pending_schedule = models.JSONField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def ordered_media(self):
        # PostMedia is ordered by position; works with prefetch_related("postmedia_set__asset").
        return [pm.asset for pm in self.postmedia_set.all()]

    def refresh_status(self, save=True):
        states = list(self.targets.exclude(status=TargetStatus.CANCELLED).values_list("status", flat=True))
        if not states:
            status = PostStatus.DRAFT
        elif all(s == TargetStatus.PUBLISHED for s in states):
            status = PostStatus.PUBLISHED
        elif all(s == TargetStatus.FAILED for s in states):
            status = PostStatus.FAILED
        elif any(s in (TargetStatus.QUEUED, TargetStatus.PUBLISHING) for s in states):
            status = PostStatus.PUBLISHING
        elif any(s == TargetStatus.UNCONFIRMED for s in states):
            status = PostStatus.UNCONFIRMED
        elif any(s == TargetStatus.SCHEDULED for s in states):
            status = PostStatus.SCHEDULED
        elif all(s == TargetStatus.DRAFT for s in states):
            status = PostStatus.DRAFT
        else:
            status = PostStatus.PARTIAL
        if status != self.status:
            self.status = status
            if save:
                self.save(update_fields=["status", "updated_at"])
        return status


class PostMedia(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE)
    asset = models.ForeignKey(MediaAsset, on_delete=models.CASCADE)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position"]


class TargetStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SCHEDULED = "scheduled", "Scheduled"
    QUEUED = "queued", "Queued"
    PUBLISHING = "publishing", "Publishing"
    PUBLISHED = "published", "Published"
    # The network may have published it but its answer was lost: a person checks before it is retried.
    UNCONFIRMED = "unconfirmed", "Unconfirmed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class FollowMode(models.TextChoices):
    DELAY = "delay", "After a delay"
    QUEUE = "queue", "Next queue slot"
    BEST_TIME = "best_time", "Best time"


class PostTarget(TimeStamped):
    """One post on one channel, with its own copy, time, outcome and metrics.

    A target can *follow* another publication (in this post or another one of the
    workspace): it waits until that one is published, then goes out with its URL in
    place of ``{url}`` (see ``scheduling.release_followers``).
    """

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="targets")
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="targets")
    # Per-channel overrides; blank means "use the post's".
    body = models.TextField(blank=True)
    title = models.CharField(max_length=300, blank=True)
    media_ids = models.JSONField(null=True, blank=True)
    options = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=16, choices=TargetStatus.choices, default=TargetStatus.DRAFT)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    # Why this time was chosen (exact / queue slot / best-time score / AI rationale).
    schedule_reason = models.CharField(max_length=500, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    queued_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    external_id = models.CharField(max_length=500, blank=True)
    external_url = models.URLField(max_length=1000, blank=True)
    last_error = models.TextField(blank=True)
    # Provider extras, e.g. {"first_comment_id": "..."}.
    extra = models.JSONField(default=dict, blank=True)

    # Waits for this publication and links to it (e.g. announce a blog post on X and Bluesky).
    follows = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="followers")
    follow_mode = models.CharField(max_length=16, choices=FollowMode.choices, default=FollowMode.DELAY)
    follow_delay = models.PositiveIntegerField(default=0, help_text="Minutes after the followed publication.")

    # Latest metrics, denormalised from analytics.MetricSnapshot for fast listing.
    metrics = models.JSONField(default=dict, blank=True)
    # Clicks on its short links, kept here so lists and analytics need no join.
    link_clicks = models.PositiveIntegerField(default=0)
    metrics_updated_at = models.DateTimeField(null=True, blank=True)
    next_metrics_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["post", "channel"], name="uniq_post_channel")]
        indexes = [
            models.Index(fields=["status", "scheduled_at"], name="target_due_idx"),
            models.Index(fields=["channel", "scheduled_at"], name="target_channel_time_idx"),
        ]
        ordering = ["scheduled_at", "id"]

    @property
    def effective_body(self):
        return self.body or self.post.body

    @property
    def effective_title(self):
        return self.title or self.post.title

    @property
    def waiting(self):
        """Scheduled, but waiting for the publication it follows."""
        return self.status == TargetStatus.SCHEDULED and self.scheduled_at is None and self.follows_id is not None

    def effective_media(self):
        if self.media_ids is None:
            return self.post.ordered_media()
        assets = {
            a.id: a for a in MediaAsset.objects.filter(workspace_id=self.post.workspace_id, id__in=self.media_ids)
        }
        return [assets[i] for i in self.media_ids if i in assets]


class ReviewAction(models.TextChoices):
    SUBMITTED = "submitted", "Submitted for review"
    APPROVED = "approved", "Approved"
    CHANGES_REQUESTED = "changes_requested", "Requested changes"
    RESET = "reset", "Edited after approval"


class PostReview(models.Model):
    """Audit trail of the approval workflow."""

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=20, choices=ReviewAction.choices)
    comment = models.TextField(blank=True)
    # A client who reviewed through a review link (no account): the name they gave.
    reviewer_name = models.CharField(max_length=120, blank=True)
    review_link = models.ForeignKey("ReviewLink", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


def _review_token():
    import secrets

    return secrets.token_urlsafe(24)


class ReviewLink(models.Model):
    """A secret link that lets someone without an account (a client) approve a post or ask for changes."""

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="review_links")
    token = models.CharField(max_length=64, unique=True, default=_review_token)
    label = models.CharField(max_length=120, blank=True)  # who it was sent to, e.g. "Acme marketing"
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def active(self):
        from django.utils import timezone

        return self.revoked_at is None and self.expires_at > timezone.now()


class PostingSlot(TimeStamped):
    """A recurring weekly time used by queue scheduling.

    Resolution for a channel: its own slots, else the slots of its teams, else
    the workspace-wide slots (no channel, no team).
    """

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="slots")
    channel = models.ForeignKey(Channel, null=True, blank=True, on_delete=models.CASCADE, related_name="slots")
    team = models.ForeignKey("teams.Team", null=True, blank=True, on_delete=models.CASCADE, related_name="slots")
    weekday = models.PositiveSmallIntegerField()  # 0 = Monday
    time = models.TimeField()
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["weekday", "time"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "channel", "team", "weekday", "time"], name="uniq_slot_v2"),
        ]

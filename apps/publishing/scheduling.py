"""Turning a scheduling intent (exact / queue / best time / now) into concrete times.

Every target gets its own ``scheduled_at``: the same post can go out on
LinkedIn at 09:10 and on Instagram at 19:40 when that is what works best for
each audience. The dispatcher (tasks.dispatch_due_targets) then fires them.
"""

from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import FollowMode, Post, PostingSlot, PostTarget, ScheduleMode, TargetStatus

ACTIVE_STATES = (TargetStatus.SCHEDULED, TargetStatus.QUEUED, TargetStatus.PUBLISHING, TargetStatus.PUBLISHED)
QUEUE_HORIZON_DAYS = 60


def taken_times(channel, exclude_target=None, around=None):
    qs = PostTarget.objects.filter(channel=channel, status__in=ACTIVE_STATES, scheduled_at__isnull=False)
    if exclude_target is not None:
        qs = qs.exclude(pk=exclude_target.pk)
    if around:
        start, end = around
        qs = qs.filter(scheduled_at__gte=start, scheduled_at__lte=end)
    return list(qs.values_list("scheduled_at", flat=True))


def _conflicts(when, taken, gap):
    return any(abs((when - t).total_seconds()) < gap.total_seconds() for t in taken)


def next_queue_slot(target, after=None):
    """First active slot (channel-specific slots win over workspace-wide ones) that is free."""
    ws = target.post.workspace
    tz = ws.tz
    after = after or timezone.now()
    slots = list(PostingSlot.objects.filter(workspace=ws, channel=target.channel, active=True))
    if not slots:
        slots = list(PostingSlot.objects.filter(workspace=ws, team__channels=target.channel, active=True).distinct())
    if not slots:
        slots = list(PostingSlot.objects.filter(workspace=ws, channel__isnull=True, team__isnull=True, active=True))
    if not slots:
        raise ValidationError({"schedule": "No posting slots configured. Add queue times in Settings → Queue."})
    taken = taken_times(
        target.channel,
        exclude_target=target,
        around=(after - timedelta(days=1), after + timedelta(days=QUEUE_HORIZON_DAYS + 1)),
    )
    local_start = after.astimezone(tz).date()
    for day in range(QUEUE_HORIZON_DAYS):
        date = local_start + timedelta(days=day)
        for slot in sorted((s for s in slots if s.weekday == date.weekday()), key=lambda s: s.time):
            when = datetime.combine(date, slot.time, tzinfo=tz)
            if when <= after:
                continue
            if _conflicts(when, taken, timedelta(minutes=1)):
                continue
            return when
    raise ValidationError({"schedule": f"The queue is full for the next {QUEUE_HORIZON_DAYS} days."})


def best_time(target, window_start=None, window_end=None):
    from apps.insights.engine import next_best_slot

    ws = target.post.workspace
    start = max(window_start or timezone.now(), timezone.now() + timedelta(minutes=5))
    end = window_end or start + timedelta(days=7)
    taken = taken_times(
        target.channel, exclude_target=target, around=(start - timedelta(days=1), end + timedelta(days=1))
    )
    slot = next_best_slot(target.channel, start, end, taken, timedelta(minutes=ws.min_gap_minutes))
    if slot is None:
        raise ValidationError({"schedule": f"No free slot for {target.channel.name} in the chosen window."})
    return slot


# --- publications that follow another one ------------------------------------------------


def follow_reason(target):
    src = target.follows
    name = src.channel.name if src else "the followed publication"
    mode = target.follow_mode
    if mode == FollowMode.QUEUE:
        when = "next queue slot after"
    elif mode == FollowMode.BEST_TIME:
        when = "best time after"
    elif target.follow_delay:
        when = f"{_minutes(target.follow_delay)} after"
    else:
        when = "right after"
    return f"Waits for {name}: {when} it is published"


def _minutes(m):
    if m % 1440 == 0:
        return f"{m // 1440} day{'s' if m >= 2880 else ''}"
    if m % 60 == 0:
        return f"{m // 60} h"
    return f"{m} min"


def follow_time(target):
    """When a follower goes out, once the publication it follows is live."""
    src = target.follows
    start = max(timezone.now(), (src.published_at or timezone.now()) + timedelta(minutes=target.follow_delay))
    try:
        if target.follow_mode == FollowMode.QUEUE:
            return next_queue_slot(target, after=start), f"Next queue slot after {src.channel.name}"
        if target.follow_mode == FollowMode.BEST_TIME:
            return best_time(target, start, start + timedelta(days=7)), f"Best time after {src.channel.name}"
    except ValidationError:
        pass  # no slot: go out at the earliest time rather than not at all
    return start, f"After {src.channel.name} was published"


def _set_follow_time(target):
    when, reason = follow_time(target)
    target.scheduled_at = when
    target.schedule_reason = reason[:500]


def release_followers(source: PostTarget):
    """Schedules the publications waiting for ``source`` (called once it is published)."""
    released = 0
    for t in source.followers.filter(status=TargetStatus.SCHEDULED, scheduled_at__isnull=True).select_related(
        "channel", "post__workspace", "follows__channel"
    ):
        if not source.external_url:
            t.status = TargetStatus.FAILED
            t.last_error = f"{source.channel.name} was published without a link to share."
            t.save(update_fields=["status", "last_error", "updated_at"])
        else:
            _set_follow_time(t)
            t.last_error = ""
            t.save(update_fields=["scheduled_at", "schedule_reason", "last_error", "updated_at"])
            released += 1
        t.post.refresh_status()
    if released:
        from .tasks import dispatch_due_targets

        transaction.on_commit(lambda: dispatch_due_targets.delay())
    return released


def note_followers(source: PostTarget, message: str, *, fail=False):
    """Tells waiting followers why they are stuck (``fail`` gives up on them)."""
    for t in source.followers.filter(status=TargetStatus.SCHEDULED, scheduled_at__isnull=True).select_related("post"):
        t.last_error = message[:4000]
        fields = ["last_error", "updated_at"]
        if fail:
            t.status = TargetStatus.FAILED
            fields.append("status")
        t.save(update_fields=fields)
        t.post.refresh_status()
        if fail:
            from apps.notifications.events import emit_for_target
            from apps.notifications.models import Event

            emit_for_target(t, Event.FAILED)


def check_follow_chain(target: PostTarget):
    """Refuses a publication that (indirectly) follows itself."""
    seen, cur = {target.pk}, target.follows
    while cur is not None:
        if cur.pk in seen:
            raise ValidationError(
                {"targets": f"{target.channel.name} cannot wait for a publication that waits for it."}
            )
        seen.add(cur.pk)
        cur = cur.follows


@transaction.atomic
def schedule_post(post: Post, mode: str, at=None, window_start=None, window_end=None, per_target=None):
    """Schedule every non-published target of ``post``.

    ``per_target`` maps target id (or channel id) → datetime, used by the
    calendar and by AI plans to pin individual channels to specific times.
    """
    now = timezone.now()
    per_target = per_target or {}
    post.schedule_mode = mode if mode != ScheduleMode.NOW else ScheduleMode.EXACT
    post.window_start, post.window_end = window_start, window_end
    post.save(update_fields=["schedule_mode", "window_start", "window_end", "updated_at"])
    targets = list(
        post.targets.select_related("channel", "post__workspace").exclude(
            status__in=(TargetStatus.PUBLISHED, TargetStatus.CANCELLED)
        )
    )
    if not targets:
        raise ValidationError({"targets": "Select at least one channel."})
    for t in targets:
        pinned = per_target.get(t.pk) or per_target.get(f"channel:{t.channel_id}")
        if t.follows_id:
            # Followers ignore the post's time: they go out after the publication they follow.
            src = t.follows
            if src.status == TargetStatus.PUBLISHED:
                when, reason = follow_time(t)
            else:
                when, reason = None, follow_reason(t)
        elif pinned:
            when, reason = pinned, "Pinned"
        elif mode == ScheduleMode.NOW:
            when, reason = now, "Publish now"
        elif mode == ScheduleMode.EXACT:
            if at is None:
                raise ValidationError({"at": "A date and time is required."})
            if at < now - timedelta(minutes=1):
                raise ValidationError({"at": "That time is in the past."})
            when, reason = at, "Exact time"
        elif mode == ScheduleMode.QUEUE:
            when, reason = next_queue_slot(t, after=window_start), "Next queue slot"
        elif mode == ScheduleMode.BEST_TIME:
            when = best_time(t, window_start, window_end)
            reason = "Best time for this channel's audience"
        else:
            raise ValidationError({"mode": "Unknown scheduling mode."})
        t.scheduled_at = when
        t.schedule_reason = reason
        t.status = TargetStatus.SCHEDULED
        t.next_attempt_at = None
        t.last_error = ""
        t.save(
            update_fields=["scheduled_at", "schedule_reason", "status", "next_attempt_at", "last_error", "updated_at"]
        )
    post.refresh_status()
    if mode == ScheduleMode.NOW:
        from .tasks import dispatch_due_targets

        transaction.on_commit(lambda: dispatch_due_targets.delay())
    return targets


@transaction.atomic
def unschedule_post(post: Post):
    post.targets.filter(status=TargetStatus.SCHEDULED).update(
        status=TargetStatus.DRAFT,
        scheduled_at=None,
        next_attempt_at=None,
        updated_at=timezone.now(),
    )
    post.refresh_status()


def reschedule_target(target: PostTarget, when):
    if target.follows_id and target.follows.status != TargetStatus.PUBLISHED:
        raise ValidationError(
            {"scheduled_at": f"This publication waits for {target.follows.channel.name}; change that in the post."}
        )
    if target.status not in (TargetStatus.DRAFT, TargetStatus.SCHEDULED, TargetStatus.FAILED):
        raise ValidationError({"scheduled_at": "This publication can no longer be moved."})
    if when < timezone.now() - timedelta(minutes=1):
        raise ValidationError({"scheduled_at": "That time is in the past."})
    target.scheduled_at = when
    target.schedule_reason = "Moved on calendar"
    target.status = TargetStatus.SCHEDULED
    target.next_attempt_at = None
    target.save(update_fields=["scheduled_at", "schedule_reason", "status", "next_attempt_at", "updated_at"])
    target.post.refresh_status()
    return target

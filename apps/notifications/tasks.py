import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import DISABLE_AFTER, Delivery, DeliveryStatus, Destination, Event
from .senders import DeliveryError, send

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
# Waits between attempts: 1 min, 5 min, 30 min, 2 h, 6 h.
BACKOFF = [60, 300, 1800, 7200, 21600]
KEEP_FOR = timedelta(days=30)


@shared_task(acks_late=True)
def deliver(delivery_id):
    with transaction.atomic():
        d = (
            Delivery.objects.select_for_update(skip_locked=True)
            .select_related("destination__workspace")
            .filter(pk=delivery_id, status=DeliveryStatus.PENDING)
            .first()
        )
        if d is None:
            return "skipped"
        d.attempts += 1
        d.next_attempt_at = None
        d.save(update_fields=["attempts", "next_attempt_at"])
    return attempt(d)


def attempt(d):
    """Sends ``d`` now and records the outcome (also used for synchronous test sends)."""
    dest = d.destination
    now = timezone.now()
    try:
        d.response_status = send(dest, d)
    except DeliveryError as exc:
        d.error = str(exc)[:2000]
        d.response_status = exc.status
        if exc.retryable and d.attempts < MAX_ATTEMPTS:
            wait = exc.retry_after or BACKOFF[min(d.attempts - 1, len(BACKOFF) - 1)]
            d.next_attempt_at = now + timedelta(seconds=min(int(wait), 6 * 3600))
        else:
            d.status = DeliveryStatus.FAILED
        d.save(update_fields=["status", "error", "response_status", "next_attempt_at"])
        _note_failure(dest, d)
        log.warning("Notification %s to %s failed: %s", d.pk, dest, exc)
        return d.status
    except Exception as exc:  # noqa: BLE001 - never retry a bug forever
        log.exception("Notification %s crashed", d.pk)
        d.status, d.error = DeliveryStatus.FAILED, f"Internal error: {exc}"[:2000]
        d.save(update_fields=["status", "error"])
        return d.status
    d.status, d.error, d.sent_at = DeliveryStatus.SENT, "", now
    d.save(update_fields=["status", "error", "response_status", "sent_at"])
    Destination.objects.filter(pk=dest.pk).update(last_sent_at=now, last_error="", failures=0)
    return d.status


def _note_failure(dest, d):
    dest.failures += 1
    dest.last_error = d.error
    fields = ["failures", "last_error", "updated_at"]
    if dest.failures >= DISABLE_AFTER and dest.enabled:
        dest.enabled = False
        dest.last_error = f"Switched off after {DISABLE_AFTER} failed deliveries in a row. Last error: {d.error}"
        fields.append("enabled")
    dest.save(update_fields=fields)


@shared_task
def retry_due_deliveries():
    due = Delivery.objects.filter(status=DeliveryStatus.PENDING, next_attempt_at__lte=timezone.now()).values_list(
        "pk", flat=True
    )[:200]
    for pk in due:
        deliver.delay(pk)
    # Queued but never picked up (broker hiccup).
    stale = Delivery.objects.filter(
        status=DeliveryStatus.PENDING,
        next_attempt_at__isnull=True,
        created_at__lt=timezone.now() - timedelta(minutes=10),
    ).values_list("pk", flat=True)[:200]
    for pk in stale:
        deliver.delay(pk)
    return len(due)


@shared_task
def prune_deliveries():
    return Delivery.objects.filter(created_at__lt=timezone.now() - KEEP_FOR).delete()[0]


@shared_task
def send_daily_digests(hour=8):
    """Hourly: workspaces where it is now ``hour`` o'clock get yesterday's summary and today's plan."""
    from apps.publishing.models import PostTarget, TargetStatus
    from apps.tenants.models import Workspace

    from .events import emit

    sent = 0
    ws_ids = (
        Destination.objects.filter(enabled=True, events__contains=[Event.DIGEST])
        .values_list("workspace_id", flat=True)
        .distinct()
    )
    for ws in Workspace.objects.filter(pk__in=list(ws_ids), archived_at__isnull=True):
        local = timezone.now().astimezone(ws.tz)
        if local.hour != hour:
            continue
        start_today = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if Delivery.objects.filter(destination__workspace=ws, event=Event.DIGEST, created_at__gte=start_today).exists():
            continue
        targets = PostTarget.objects.filter(post__workspace=ws)
        yesterday = (start_today - timedelta(days=1), start_today)
        published = targets.filter(status=TargetStatus.PUBLISHED, published_at__range=yesterday).count()
        failed = targets.filter(status=TargetStatus.FAILED, updated_at__range=yesterday).count()
        unconfirmed = targets.filter(status=TargetStatus.UNCONFIRMED).count()
        today = (
            targets.filter(
                status=TargetStatus.SCHEDULED, scheduled_at__range=(start_today, start_today + timedelta(days=1))
            )
            .select_related("channel", "post")
            .order_by("scheduled_at")
        )
        lines = [f"Yesterday: {published} published, {failed} failed."]
        if unconfirmed:
            lines.append(f"{unconfirmed} publication(s) need checking.")
        lines.append(f"Scheduled today: {today.count()}.")
        for t in today[:8]:
            title = t.post.title or (t.post.body or "").strip().split("\n")[0]
            lines.append(f"{t.scheduled_at.astimezone(ws.tz):%H:%M} {t.channel.name}: {title[:60]}")
        emit(
            ws,
            Event.DIGEST,
            {
                "date": local.date().isoformat(),
                "published_yesterday": published,
                "failed_yesterday": failed,
                "needs_checking": unconfirmed,
                "scheduled_today": today.count(),
                "lines": lines,
            },
        )
        sent += 1
    return sent

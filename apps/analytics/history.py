"""Importing a channel's past posts as published posts, so analytics and best times start with data."""

import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.channels.providers import ProviderError
from apps.publishing.models import Post, PostTarget, TargetStatus

from .models import HistoryImport
from .tasks import record

log = logging.getLogger(__name__)
MAX = 500


@shared_task
def import_history(import_id):
    job = HistoryImport.objects.select_related("channel", "workspace").filter(pk=import_id).first()
    if job is None or job.status not in ("queued", "failed"):
        return "skipped"
    job.status = "running"
    job.save(update_fields=["status"])
    channel = job.channel
    provider = channel.get_provider()
    try:
        provider.refresh_credentials()
        entries = provider.fetch_history(min(job.limit, MAX))
    except ProviderError as exc:
        job.status, job.error, job.finished_at = "failed", str(exc)[:2000], timezone.now()
        job.save(update_fields=["status", "error", "finished_at"])
        return "failed"
    known = set(PostTarget.objects.filter(channel=channel).values_list("external_id", flat=True))
    now = timezone.now()
    imported = 0
    for e in entries:
        if not e.external_id or e.external_id in known or e.published_at is None:
            continue
        with transaction.atomic():
            post = Post.objects.create(
                workspace=job.workspace,
                author=job.requested_by,
                title=e.title[:300],
                body=e.text,
                imported=True,
                status="published",
            )
            target = PostTarget.objects.create(
                post=post,
                channel=channel,
                status=TargetStatus.PUBLISHED,
                published_at=e.published_at,
                external_id=e.external_id[:500],
                external_url=e.url[:1000],
                extra={"imported": True},
                # Without numbers in the listing, the metrics collector fetches them next.
                next_metrics_at=None if e.metrics else now,
            )
            if e.metrics:
                record(target, e.metrics, now, followers=channel.followers_count)
        known.add(e.external_id)
        imported += 1
    job.status, job.found, job.imported, job.finished_at = "done", len(entries), imported, timezone.now()
    job.save(update_fields=["status", "found", "imported", "finished_at"])
    if imported:
        _rebuild_best_times(channel)
    return imported


def _rebuild_best_times(channel):
    from apps.insights.engine import compute_grid
    from apps.insights.models import BestTimeProfile

    try:
        scores, n, counts = compute_grid(channel)
        BestTimeProfile.objects.update_or_create(
            channel=channel,
            defaults={"scores": scores, "counts": counts, "sample_size": n, "computed_at": timezone.now()},
        )
    except Exception:  # noqa: BLE001 - best times are rebuilt nightly anyway
        log.exception("Could not rebuild best times for channel %s", channel.pk)

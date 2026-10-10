"""Metric collection on a decaying cadence: 1h, 6h, 24h, 3d, 7d, 30d after publishing."""

import logging
from collections import defaultdict
from dataclasses import asdict
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.channels.models import Channel, ChannelStatus
from apps.channels.providers import ProviderError
from apps.publishing.models import PostTarget, TargetStatus

from .models import ENGAGEMENT_FIELDS, METRIC_FIELDS, AudienceSnapshot, MetricSnapshot

log = logging.getLogger(__name__)
CHECKPOINTS_H = (1, 6, 24, 72, 168, 720)


def next_checkpoint(published_at, now):
    age_h = (now - published_at).total_seconds() / 3600
    for h in CHECKPOINTS_H:
        if h > age_h + 0.25:
            return published_at + timedelta(hours=h)
    return None


def record(target, metrics, now, followers=None):
    values = {f: getattr(metrics, f) for f in METRIC_FIELDS}
    engagements = sum(values[f] or 0 for f in ENGAGEMENT_FIELDS)
    denominator = values["impressions"] or values["reach"] or followers
    rate = (engagements / denominator) if denominator else None
    snap = MetricSnapshot.objects.create(
        target=target,
        captured_at=now,
        age_hours=(now - target.published_at).total_seconds() / 3600,
        engagements=engagements,
        engagement_rate=rate,
        raw=asdict(metrics)["raw"],
        **values,
    )
    target.metrics = {k: v for k, v in snap.as_dict().items() if v is not None}
    target.metrics_updated_at = now
    target.next_metrics_at = next_checkpoint(target.published_at, now)
    target.save(update_fields=["metrics", "metrics_updated_at", "next_metrics_at", "updated_at"])
    return snap


@shared_task
def collect_due_metrics(limit=500):
    now = timezone.now()
    due = (
        PostTarget.objects.select_related("channel")
        .filter(status=TargetStatus.PUBLISHED, next_metrics_at__lte=now, channel__status=ChannelStatus.ACTIVE)
        .exclude(external_id="")
        .order_by("next_metrics_at")[:limit]
    )
    by_channel = defaultdict(list)
    for t in due:
        by_channel[t.channel].append(t)
    for channel, targets in by_channel.items():
        collect_channel_metrics(channel, targets, now)
    return sum(len(v) for v in by_channel.values())


def collect_channel_metrics(channel, targets, now=None):
    now = now or timezone.now()
    provider = channel.get_provider()
    try:
        provider.refresh_credentials()
        results = provider.fetch_metrics([t.external_id for t in targets])
    except ProviderError as exc:
        log.warning("Metrics for channel %s failed: %s", channel.pk, exc)
        if exc.auth:
            channel.mark_needs_reauth(str(exc))
            return
        # Try again later without hammering a rate-limited API.
        delay = timedelta(seconds=exc.retry_after or 1800)
        PostTarget.objects.filter(pk__in=[t.pk for t in targets]).update(next_metrics_at=now + delay)
        return
    for t in targets:
        m = results.get(t.external_id)
        if m is None:
            t.next_metrics_at = next_checkpoint(t.published_at, now)
            t.save(update_fields=["next_metrics_at"])
            continue
        record(t, m, now, followers=channel.followers_count)


@shared_task
def snapshot_channel_audiences():
    today = timezone.now().date()
    for channel in Channel.objects.filter(status=ChannelStatus.ACTIVE):
        try:
            followers = channel.get_provider().fetch_audience()
        except ProviderError as exc:
            log.info("Audience for channel %s unavailable: %s", channel.pk, exc)
            continue
        if followers is None:
            continue
        channel.followers_count = followers
        channel.save(update_fields=["followers_count", "updated_at"])
        AudienceSnapshot.objects.update_or_create(channel=channel, date=today, defaults={"followers": followers})

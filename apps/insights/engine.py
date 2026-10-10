"""Best-time-to-post engine.

For each channel we estimate how well a post performs depending on the local
weekday and hour it goes out, as a 7×24 grid of relative scores.

* Prior: a per-platform curve of typical audience activity (hand-tuned from
  public industry studies). It is what new channels get.
* Evidence: the engagement rate of every post published on the channel in the
  last 180 days, measured at its ~24h snapshot so old and new posts compare.
* Blend: in log-rate space, per bucket, a Bayesian shrinkage of the observed
  mean towards "channel average nudged by the prior curve", weighted by how
  many posts landed in that bucket (``PRIOR_WEIGHT`` pseudo-observations).
  The prior is smoothed over neighbouring hours since audiences do not change
  abruptly from 10:59 to 11:00.

Scores are normalised to 0–100 within the channel.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta

from django.utils import timezone

PRIOR_WEIGHT = 3.0
# How far the platform curve moves the expectation of an unobserved hour (log units).
PRIOR_STRENGTH = 0.5
LOOKBACK_DAYS = 180
MIN_GAP_DEFAULT = timedelta(minutes=90)
# Avoid everyone posting at :00 — pick a minute inside the hour.
MINUTE_OFFSETS = (7, 22, 37, 52)

# Hourly activity shapes (index = local hour).
_WORKDAY = [
    0.15,
    0.1,
    0.08,
    0.08,
    0.1,
    0.2,
    0.45,
    0.8,
    1.15,
    1.3,
    1.25,
    1.2,
    1.35,
    1.25,
    1.1,
    1.05,
    1.05,
    1.1,
    1.0,
    0.9,
    0.85,
    0.7,
    0.5,
    0.3,
]
_EVENING = [
    0.2,
    0.12,
    0.08,
    0.08,
    0.1,
    0.2,
    0.45,
    0.75,
    0.9,
    0.95,
    1.0,
    1.1,
    1.2,
    1.1,
    1.0,
    1.0,
    1.05,
    1.15,
    1.3,
    1.4,
    1.45,
    1.3,
    0.9,
    0.45,
]
_READING = [
    0.2,
    0.12,
    0.1,
    0.1,
    0.12,
    0.25,
    0.6,
    1.1,
    1.4,
    1.45,
    1.35,
    1.2,
    1.15,
    1.1,
    1.05,
    1.0,
    0.95,
    0.9,
    0.85,
    0.85,
    0.8,
    0.65,
    0.45,
    0.3,
]

# Weekday multipliers, Monday first.
_WEEKDAYS = {
    "office": [1.0, 1.15, 1.2, 1.15, 0.95, 0.45, 0.4],
    "social": [1.0, 1.05, 1.05, 1.05, 1.0, 0.85, 0.85],
    "visual": [0.95, 1.0, 1.1, 1.05, 1.1, 0.95, 0.9],
    "reading": [1.05, 1.15, 1.1, 1.1, 0.9, 0.6, 0.7],
}

PRIORS = {
    "linkedin": (_WORKDAY, "office"),
    "x": (_WORKDAY, "social"),
    "bluesky": (_WORKDAY, "social"),
    "mastodon": (_WORKDAY, "social"),
    "facebook": (_EVENING, "social"),
    "instagram": (_EVENING, "visual"),
    "telegram": (_EVENING, "social"),
    "whatsapp": (_EVENING, "social"),
    "qq": (_EVENING, "social"),
    "wechat": (_EVENING, "reading"),
    "threads": (_EVENING, "social"),
    "youtube": (_EVENING, "visual"),
    "discord": (_EVENING, "social"),
    "ghost": (_READING, "reading"),
    "brevo": (_READING, "reading"),
    "listmonk": (_READING, "reading"),
    "webhook": (_WORKDAY, "social"),
    "wordpress": (_READING, "reading"),
    "drupal": (_READING, "reading"),
    "medium": (_READING, "reading"),
    "hugo": (_READING, "reading"),
}


def prior_grid(provider: str) -> list[list[float]]:
    hours, week = PRIORS.get(provider, (_WORKDAY, "social"))
    return [[hours[h] * _WEEKDAYS[week][d] for h in range(24)] for d in range(7)]


def _smooth(grid):
    out = [[0.0] * 24 for _ in range(7)]
    for d in range(7):
        for h in range(24):
            prev_d, prev_h = (d, h - 1) if h else ((d - 1) % 7, 23)
            next_d, next_h = (d, h + 1) if h < 23 else ((d + 1) % 7, 0)
            out[d][h] = 0.25 * grid[prev_d][prev_h] + 0.5 * grid[d][h] + 0.25 * grid[next_d][next_h]
    return out


def _normalise(grid):
    flat = [v for row in grid for v in row]
    lo, hi = min(flat), max(flat)
    span = (hi - lo) or 1.0
    return [[round(100 * (v - lo) / span, 1) for v in row] for row in grid]


def observations(channel, now=None):
    """(local weekday, local hour, engagement rate) for each recent post on the channel."""
    from apps.analytics.models import MetricSnapshot
    from apps.publishing.models import TargetStatus

    now = now or timezone.now()
    tz = channel.tz
    snaps = MetricSnapshot.objects.filter(
        target__channel=channel,
        target__status=TargetStatus.PUBLISHED,
        target__published_at__gte=now - timedelta(days=LOOKBACK_DAYS),
        engagement_rate__isnull=False,
    ).values("target_id", "target__published_at", "age_hours", "engagement_rate")
    # Prefer the snapshot closest to 24h of age for each post.
    best = {}
    for s in snaps:
        cur = best.get(s["target_id"])
        if cur is None or abs(s["age_hours"] - 24) < abs(cur["age_hours"] - 24):
            best[s["target_id"]] = s
    out = []
    for s in best.values():
        local = s["target__published_at"].astimezone(tz)
        out.append((local.weekday(), local.hour, s["engagement_rate"]))
    return out


def compute_grid(channel, now=None):
    """Return (scores 7×24 0–100, sample size, per-bucket counts)."""
    prior = _smooth(prior_grid(channel.provider))
    obs = observations(channel, now)
    if not obs:
        return _normalise(prior), 0, [[0] * 24 for _ in range(7)]
    # Work in log space: rates are heavy-tailed (one viral post) and the prior
    # should nudge, not multiply, what the channel has actually shown.
    buckets = defaultdict(list)
    for d, h, r in obs:
        buckets[(d, h)].append(math.log1p(r * 100))
    log_mean = sum(v for vals in buckets.values() for v in vals) / len(obs)
    prior_log_mean = sum(math.log(v) for row in prior for v in row) / (7 * 24)
    grid, counts = [], []
    for d in range(7):
        row, crow = [], []
        for h in range(24):
            expected = log_mean + PRIOR_STRENGTH * (math.log(prior[d][h]) - prior_log_mean)
            vals = buckets.get((d, h), [])
            n = len(vals)
            row.append((sum(vals) + PRIOR_WEIGHT * expected) / (n + PRIOR_WEIGHT))
            crow.append(n)
        grid.append(row)
        counts.append(crow)
    return _normalise(grid), len(obs), counts


def get_profile(channel, max_age=timedelta(hours=24)):
    from .models import BestTimeProfile

    profile = BestTimeProfile.objects.filter(channel=channel).first()
    if profile is None or timezone.now() - profile.computed_at > max_age:
        scores, n, counts = compute_grid(channel)
        profile, _ = BestTimeProfile.objects.update_or_create(
            channel=channel,
            defaults={"scores": scores, "counts": counts, "sample_size": n, "computed_at": timezone.now()},
        )
    return profile


def top_slots(scores, k=5):
    flat = sorted(((scores[d][h], d, h) for d in range(7) for h in range(24)), reverse=True)
    return [{"weekday": d, "hour": h, "score": s} for s, d, h in flat[:k]]


def next_best_slot(
    channel, start: datetime, end: datetime, taken: list[datetime], min_gap: timedelta = MIN_GAP_DEFAULT
) -> datetime | None:
    """Highest-scoring free moment in [start, end], respecting a gap to other posts.

    Ties go to the earliest moment, and a mild decay favours sooner slots so a
    post is not pushed a week out for a 1-point gain.
    """
    scores = get_profile(channel).scores
    tz = channel.tz
    cursor = start.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    best, best_value = None, -1.0
    span_h = max((end - start).total_seconds() / 3600, 1)
    i = 0
    while cursor <= end:
        minute = MINUTE_OFFSETS[(channel.pk + cursor.hour) % len(MINUTE_OFFSETS)]
        when = cursor.replace(minute=minute)
        if start <= when <= end and not any(abs((when - t).total_seconds()) < min_gap.total_seconds() for t in taken):
            decay = 1 - 0.1 * (i / span_h)
            value = scores[when.weekday()][when.hour] * decay
            if value > best_value:
                best, best_value = when, value
        cursor += timedelta(hours=1)
        i += 1
    return best


def describe_slot(when: datetime, scores) -> str:
    s = scores[when.weekday()][when.hour]
    return f"{when:%A %H:%M} (score {s:.0f}/100)"

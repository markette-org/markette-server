"""The analytics overview of a set of publications (used by the API, reports and shared report pages)."""

from datetime import timedelta

from django.utils import timezone

from apps.publishing.models import PostTarget, TargetStatus

SUMMED = ("impressions", "reach", "likes", "comments", "shares", "saves", "clicks")


def build_overview(ws, qs, d0, d1):
    rows = list(
        qs.select_related("channel", "post").only(
            "id",
            "metrics",
            "link_clicks",
            "published_at",
            "external_url",
            "channel__id",
            "channel__name",
            "channel__provider",
            "post__id",
            "post__title",
            "post__body",
            "body",
        )
    )
    totals = {k: 0 for k in SUMMED}
    totals["link_clicks"] = 0
    per_channel, per_day = {}, {}
    tz = ws.tz
    for t in rows:
        m = t.metrics or {}
        ch = per_channel.setdefault(
            t.channel_id,
            {
                "channel": t.channel_id,
                "name": t.channel.name,
                "provider": t.channel.provider,
                "posts": 0,
                "engagements": 0,
                "link_clicks": 0,
                **{k: 0 for k in SUMMED},
                "rates": [],
            },
        )
        day = t.published_at.astimezone(tz).date().isoformat()
        dd = per_day.setdefault(day, {"date": day, "posts": 0, "engagements": 0, "impressions": 0})
        ch["posts"] += 1
        dd["posts"] += 1
        for k in SUMMED:
            v = m.get(k) or 0
            totals[k] += v
            ch[k] += v
        totals["link_clicks"] += t.link_clicks
        ch["link_clicks"] += t.link_clicks
        eng = m.get("engagements") or 0
        ch["engagements"] += eng
        dd["engagements"] += eng
        dd["impressions"] += m.get("impressions") or 0
        if m.get("engagement_rate") is not None:
            ch["rates"].append(m["engagement_rate"])
    for ch in per_channel.values():
        rates = ch.pop("rates")
        ch["avg_engagement_rate"] = sum(rates) / len(rates) if rates else None
    days, d = [], d0
    while d <= d1:
        days.append(per_day.get(d.isoformat(), {"date": d.isoformat(), "posts": 0, "engagements": 0, "impressions": 0}))
        d += timedelta(days=1)
    top = sorted(rows, key=lambda t: (t.metrics or {}).get("engagements") or 0, reverse=True)[:10]
    upcoming = PostTarget.objects.filter(post__workspace=ws, status=TargetStatus.SCHEDULED).count()
    failed = PostTarget.objects.filter(
        post__workspace=ws, status=TargetStatus.FAILED, updated_at__gte=timezone.now() - timedelta(days=7)
    ).count()
    return {
        "from": d0,
        "to": d1,
        "totals": {
            **totals,
            "posts": len(rows),
            "engagements": sum(c["engagements"] for c in per_channel.values()),
        },
        "scheduled": upcoming,
        "failed_last_7d": failed,
        "channels": sorted(per_channel.values(), key=lambda c: -c["engagements"]),
        "daily": days,
        "top_posts": [
            {
                "target": t.id,
                "post": t.post_id,
                "title": t.post.title or (t.body or t.post.body)[:80],
                "channel": t.channel.name,
                "provider": t.channel.provider,
                "url": t.external_url,
                "published_at": t.published_at,
                "metrics": t.metrics,
                "link_clicks": t.link_clicks,
            }
            for t in top
        ],
    }

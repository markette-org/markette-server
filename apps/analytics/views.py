from datetime import timedelta

from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from apps.core.docs import doc, query
from apps.publishing.models import PostTarget, TargetStatus
from apps.teams.access import filter_channels
from apps.tenants.tenancy import WorkspaceRolePermission, resolve_membership

from .models import AudienceSnapshot, MetricSnapshot
from .tasks import collect_channel_metrics

SUMMED = ("impressions", "reach", "likes", "comments", "shares", "saves", "clicks")


def _range(request, ws):
    tz = ws.tz
    today = timezone.now().astimezone(tz).date()
    start = parse_date(request.query_params.get("from") or "") or today - timedelta(days=29)
    end = parse_date(request.query_params.get("to") or "") or today
    if end < start:
        raise ValidationError("'to' is before 'from'.")
    from datetime import datetime, time

    return (
        datetime.combine(start, time.min, tzinfo=tz),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz),
        start,
        end,
    )


class AnalyticsViewSet(ViewSet):
    permission_classes = [WorkspaceRolePermission]

    def _targets(self, request):
        ws = resolve_membership(request).workspace
        start, end, d0, d1 = _range(request, ws)
        qs = PostTarget.objects.filter(
            post__workspace=ws, status=TargetStatus.PUBLISHED, published_at__gte=start, published_at__lt=end
        )
        qs = filter_channels(qs, resolve_membership(request), "channel__")
        if team := request.query_params.get("team"):
            qs = qs.filter(channel__teams=team)
        if channels := request.query_params.get("channels"):
            qs = qs.filter(channel_id__in=[int(c) for c in channels.split(",") if c.isdigit()])
        if campaign := request.query_params.get("campaign"):
            qs = qs.filter(post__campaign_id=campaign)
        return ws, qs, (start, end, d0, d1)

    @staticmethod
    def _metric(name):
        return Coalesce(F(f"metrics__{name}"), 0)

    @doc(
        "analytics",
        "Totals, per-channel performance, daily series and top posts",
        params=[
            query("from", "Start date YYYY-MM-DD (default: 30 days ago)"),
            query("to", "End date YYYY-MM-DD (default: today)"),
            query("channels", "Comma-separated channel ids"),
            query("team", "Team id"),
            query("campaign", "Campaign id"),
        ],
    )
    @action(detail=False)
    def overview(self, request):
        ws, qs, (start, end, d0, d1) = self._targets(request)
        rows = list(
            qs.select_related("channel", "post").only(
                "id",
                "metrics",
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
            days.append(
                per_day.get(d.isoformat(), {"date": d.isoformat(), "posts": 0, "engagements": 0, "impressions": 0})
            )
            d += timedelta(days=1)
        top = sorted(rows, key=lambda t: (t.metrics or {}).get("engagements") or 0, reverse=True)[:10]
        upcoming = PostTarget.objects.filter(post__workspace=ws, status=TargetStatus.SCHEDULED).count()
        failed = PostTarget.objects.filter(
            post__workspace=ws, status=TargetStatus.FAILED, updated_at__gte=timezone.now() - timedelta(days=7)
        ).count()
        return Response(
            {
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
                    }
                    for t in top
                ],
            }
        )

    @doc("analytics", "Metric history of one publication")
    @action(detail=False, url_path=r"targets/(?P<target_id>\d+)")
    def target(self, request, target_id=None):
        ws = resolve_membership(request).workspace
        t = PostTarget.objects.filter(pk=target_id, post__workspace=ws).select_related("channel").first()
        if t is None:
            raise NotFound("Unknown publication.")
        snaps = MetricSnapshot.objects.filter(target=t)
        return Response(
            {"target": t.id, "channel": t.channel.name, "metrics": t.metrics, "series": [s.as_dict() for s in snaps]}
        )

    @doc("analytics", "Fetch fresh metrics for one publication now")
    @action(detail=False, methods=["post"], url_path=r"targets/(?P<target_id>\d+)/refresh")
    def refresh_target(self, request, target_id=None):
        ws = resolve_membership(request).workspace
        t = (
            PostTarget.objects.filter(pk=target_id, post__workspace=ws, status=TargetStatus.PUBLISHED)
            .select_related("channel")
            .first()
        )
        if t is None:
            raise NotFound("Unknown publication.")
        collect_channel_metrics(t.channel, [t])
        t.refresh_from_db()
        return Response({"target": t.id, "metrics": t.metrics})

    @doc("analytics", "Follower counts over time", params=[query("from", "YYYY-MM-DD"), query("to", "YYYY-MM-DD")])
    @action(detail=False)
    def audience(self, request):
        ws = resolve_membership(request).workspace
        start, end, d0, d1 = _range(request, ws)
        qs = AudienceSnapshot.objects.filter(channel__organization_id=ws.organization_id, date__gte=d0, date__lte=d1)
        qs = filter_channels(qs, resolve_membership(request), "channel__")
        out = {}
        for s in qs.select_related("channel"):
            out.setdefault(s.channel_id, {"channel": s.channel_id, "name": s.channel.name, "points": []})
            out[s.channel_id]["points"].append({"date": s.date, "followers": s.followers})
        return Response(list(out.values()))

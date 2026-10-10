from datetime import datetime, time, timedelta

from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from apps.channels.models import Channel
from apps.core.docs import doc, query
from apps.platform import conf
from apps.publishing.models import Campaign, Post
from apps.teams.access import filter_channels
from apps.tenants.tenancy import WorkspaceRolePermission, resolve_membership

from . import ai, engine


class SuggestTimesRequestSerializer(serializers.Serializer):
    channels = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)
    post = serializers.IntegerField(required=False)
    body = serializers.CharField(required=False, allow_blank=True)
    title = serializers.CharField(required=False, allow_blank=True)
    window_start = serializers.DateTimeField(required=False, allow_null=True)
    window_end = serializers.DateTimeField(required=False, allow_null=True)
    campaign = serializers.IntegerField(required=False, allow_null=True)
    use_ai = serializers.BooleanField(default=True)


class CampaignPlanRequestSerializer(serializers.Serializer):
    channels = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)
    brief = serializers.CharField()
    start = serializers.DateField()
    end = serializers.DateField()
    posts_per_week = serializers.IntegerField(min_value=1, max_value=50, default=3)
    language = serializers.CharField(required=False, allow_blank=True)
    campaign = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs["end"] < attrs["start"]:
            raise serializers.ValidationError({"end": "The period ends before it starts."})
        if attrs["end"] - attrs["start"] > timedelta(days=92):
            raise serializers.ValidationError({"end": "Plan at most three months at a time."})
        return attrs


class AdaptRequestSerializer(serializers.Serializer):
    channels = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)
    body = serializers.CharField()
    title = serializers.CharField(required=False, allow_blank=True)
    instructions = serializers.CharField(required=False, allow_blank=True)


class InsightsViewSet(ViewSet):
    permission_classes = [WorkspaceRolePermission]

    def _ws(self, request):
        return resolve_membership(request).workspace

    def _channels(self, ws, ids):
        qs = filter_channels(
            Channel.objects.filter(organization_id=ws.organization_id, id__in=ids), resolve_membership(self.request)
        )
        channels = list(qs.select_related("organization"))
        if len(channels) != len(set(ids)):
            raise ValidationError({"channels": "Unknown channel."})
        return channels

    def _ai_allowed(self, ws):
        if not (conf.get("AI_ENABLED") and ws.ai_enabled):
            raise ValidationError({"detail": "AI assistance is disabled for this workspace."})

    def get_permissions(self):
        # AI endpoints create nothing but spend tokens, so they need the editor role even though they are POSTs.
        if getattr(self, "action", None) in ("suggest", "plan", "adapt"):
            self.required_role = "editor"
        return super().get_permissions()

    @doc(
        "insights",
        "Best times to post per channel",
        description="7×24 scores (Monday first, local hours, 0–100) learned from each channel's engagement.",
        params=[query("channels", "Comma-separated channel ids")],
    )
    @action(detail=False, url_path="best-times")
    def best_times(self, request):
        ws = self._ws(request)
        ids = [int(i) for i in (request.query_params.get("channels") or "").split(",") if i.isdigit()]
        qs = filter_channels(Channel.objects.filter(organization_id=ws.organization_id), resolve_membership(request))
        qs = qs.select_related("organization")
        if ids:
            qs = qs.filter(id__in=ids)
        out = []
        for ch in qs:
            p = engine.get_profile(ch)
            out.append(
                {
                    "channel": ch.id,
                    "name": ch.name,
                    "provider": ch.provider,
                    "scores": p.scores,
                    "counts": p.counts,
                    "sample_size": p.sample_size,
                    "computed_at": p.computed_at,
                    "top": engine.top_slots(p.scores),
                    "source": "data" if p.sample_size >= 10 else ("blended" if p.sample_size else "prior"),
                }
            )
        return Response(
            {"timezone": ws.timezone, "channels": out, "ai_enabled": conf.get("AI_ENABLED") and ws.ai_enabled}
        )

    @doc("insights", "Recompute best times now")
    @action(detail=False, methods=["post"], url_path="best-times/rebuild")
    def rebuild(self, request):
        ws = self._ws(request)
        for ch in Channel.objects.filter(organization_id=ws.organization_id).select_related("organization"):
            engine.get_profile(ch, max_age=timedelta(0))
        return self.best_times(request)

    @doc("insights", "Suggest a publishing time per channel", request=SuggestTimesRequestSerializer)
    @action(detail=False, methods=["post"])
    def suggest(self, request):
        ws = self._ws(request)
        s = SuggestTimesRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        channels = self._channels(ws, d["channels"])
        body, title = d.get("body", ""), d.get("title", "")
        if d.get("post"):
            post = Post.objects.filter(workspace=ws, pk=d["post"]).first()
            if post is None:
                raise ValidationError({"post": "Unknown post."})
            body, title = body or post.body, title or post.title
        campaign = Campaign.objects.filter(workspace=ws, pk=d.get("campaign")).first() if d.get("campaign") else None
        start, end = d.get("window_start"), d.get("window_end")
        if d["use_ai"] and conf.get("AI_ENABLED") and ws.ai_enabled and body.strip():
            try:
                return Response(ai.suggest_times(ws, request.user, channels, body, title, start, end, campaign))
            except ai.AIUnavailable as exc:
                note = f"{exc} Showing the statistical best times instead."
        else:
            note = "Statistical best times for each channel."
        from django.utils import timezone

        from apps.publishing.scheduling import taken_times

        now = timezone.now()
        start = max(start or now, now + timedelta(minutes=5))
        end = end or start + timedelta(days=7)
        out = []
        for ch in channels:
            slot = engine.next_best_slot(
                ch,
                start,
                end,
                taken_times(ch, around=(start - timedelta(days=1), end)),
                timedelta(minutes=ws.min_gap_minutes),
            )
            if slot:
                out.append(
                    {
                        "channel": ch.id,
                        "scheduled_at": slot,
                        "source": "engine",
                        "rationale": engine.describe_slot(slot, engine.get_profile(ch).scores),
                    }
                )
        return Response({"summary": note, "suggestions": out})

    @doc("insights", "Plan a campaign with AI", request=CampaignPlanRequestSerializer)
    @action(detail=False, methods=["post"])
    def plan(self, request):
        ws = self._ws(request)
        self._ai_allowed(ws)
        s = CampaignPlanRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        channels = self._channels(ws, d["channels"])
        tz = ws.tz
        start = datetime.combine(d["start"], time.min, tzinfo=tz)
        end = datetime.combine(d["end"], time(23, 59), tzinfo=tz)
        brief = d["brief"]
        if d.get("campaign"):
            c = Campaign.objects.filter(workspace=ws, pk=d["campaign"]).first()
            if c and c.brief and c.brief not in brief:
                brief = f"{c.name}\n{c.brief}\n\n{brief}"
        try:
            return Response(
                ai.plan_campaign(
                    ws, request.user, channels, brief, start, end, d["posts_per_week"], d.get("language", "")
                )
            )
        except ai.AIUnavailable as exc:
            raise ValidationError({"detail": str(exc)}) from exc

    @doc("insights", "Adapt a text to each network with AI", request=AdaptRequestSerializer)
    @action(detail=False, methods=["post"])
    def adapt(self, request):
        ws = self._ws(request)
        self._ai_allowed(ws)
        s = AdaptRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        try:
            return Response(
                ai.adapt_content(
                    ws,
                    request.user,
                    self._channels(ws, d["channels"]),
                    d["body"],
                    d.get("title", ""),
                    d.get("instructions", ""),
                )
            )
        except ai.AIUnavailable as exc:
            raise ValidationError({"detail": str(exc)}) from exc

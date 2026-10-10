"""Platform administration for superadmins (``User.is_superuser``).

Everything here is cross-tenant and read-mostly: overview, organizations and
their limits, users, publishing health and AI usage. Superadmins do not see
tenant content through these endpoints beyond what is needed to operate it.
"""

import secrets
import zoneinfo
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, permissions, serializers, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.channels.models import Channel, ChannelStatus
from apps.core.docs import doc, query
from apps.insights.models import AIRequest
from apps.platform import conf
from apps.platform.models import PlatformSettings
from apps.publishing.models import PostTarget, TargetStatus
from apps.tenants import limits
from apps.tenants.models import Invitation, Organization, OrgMembership, OrgRole, Workspace

User = get_user_model()


class IsSuperAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


def ai_cost(input_tokens, output_tokens):
    return round(
        (input_tokens or 0) / 1e6 * conf.get("AI_PRICE_INPUT_PER_MTOK")
        + (output_tokens or 0) / 1e6 * conf.get("AI_PRICE_OUTPUT_PER_MTOK"),
        4,
    )


# Aggregates that split provider-reported cost (OpenRouter) from token counts still to be estimated.
COST_AGG = {
    "reported": Sum("cost_usd"),
    "est_i": Sum("input_tokens", filter=Q(cost_usd__isnull=True)),
    "est_o": Sum("output_tokens", filter=Q(cost_usd__isnull=True)),
}


def total_cost(row):
    return round(float(row.get("reported") or 0) + ai_cost(row.get("est_i"), row.get("est_o")), 4)


@doc("platform", "Installation overview")
@api_view(["GET"])
@permission_classes([IsSuperAdmin])
def overview(request):
    now = timezone.now()
    day, week = now - timedelta(days=1), now - timedelta(days=7)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ai = AIRequest.objects.filter(created_at__gte=month_start).aggregate(
        i=Sum("input_tokens"), o=Sum("output_tokens"), n=Count("id"), **COST_AGG
    )
    published = PostTarget.objects.filter(status=TargetStatus.PUBLISHED)
    daily = (
        published.filter(published_at__gte=now - timedelta(days=30))
        .annotate(d=TruncDate("published_at"))
        .values("d")
        .annotate(n=Count("id"))
        .order_by("d")
    )
    return Response(
        {
            "organizations": {
                "total": Organization.objects.count(),
                "suspended": Organization.objects.filter(status="suspended").count(),
                "new_7d": Organization.objects.filter(created_at__gte=week).count(),
            },
            "workspaces": Workspace.objects.filter(archived_at__isnull=True).count(),
            "users": {
                "total": User.objects.count(),
                "active_7d": User.objects.filter(last_login__gte=week).count(),
                "new_7d": User.objects.filter(date_joined__gte=week).count(),
            },
            "channels": {
                "total": Channel.objects.count(),
                "needs_reauth": Channel.objects.filter(status=ChannelStatus.NEEDS_REAUTH).count(),
                "by_provider": dict(
                    Channel.objects.values_list("provider").annotate(n=Count("id")).order_by("provider")
                ),
            },
            "publishing": {
                "published_24h": published.filter(published_at__gte=day).count(),
                "published_7d": published.filter(published_at__gte=week).count(),
                "failed_24h": PostTarget.objects.filter(status=TargetStatus.FAILED, updated_at__gte=day).count(),
                "scheduled": PostTarget.objects.filter(status=TargetStatus.SCHEDULED).count(),
                "daily": [{"date": r["d"], "published": r["n"]} for r in daily],
            },
            "ai_this_month": {
                "requests": ai["n"],
                "input_tokens": ai["i"] or 0,
                "output_tokens": ai["o"] or 0,
                "cost_usd": total_cost(ai),
            },
        }
    )


@doc("platform", "Publishing health")
@api_view(["GET"])
@permission_classes([IsSuperAdmin])
def health(request):
    """Is the scheduler keeping up, and what is failing?"""
    now = timezone.now()
    overdue = PostTarget.objects.filter(
        status=TargetStatus.SCHEDULED,
        scheduled_at__lte=now - timedelta(minutes=2),
        next_attempt_at__isnull=True,
        channel__status=ChannelStatus.ACTIVE,
        post__workspace__archived_at__isnull=True,
        post__workspace__organization__status="active",
    )
    oldest = overdue.aggregate(m=Min("scheduled_at"))["m"]
    stuck = PostTarget.objects.filter(
        status__in=(TargetStatus.QUEUED, TargetStatus.PUBLISHING), updated_at__lte=now - timedelta(minutes=10)
    ).count()
    failures = (
        PostTarget.objects.filter(status=TargetStatus.FAILED, updated_at__gte=now - timedelta(days=7))
        .select_related("channel", "post__workspace__organization")
        .order_by("-updated_at")[:50]
    )
    errors_by_provider = (
        PostTarget.objects.filter(status=TargetStatus.FAILED, updated_at__gte=now - timedelta(days=7))
        .values("channel__provider")
        .annotate(n=Count("id"))
        .order_by("-n")
    )
    last_publish = PostTarget.objects.filter(status=TargetStatus.PUBLISHED).aggregate(m=Max("published_at"))["m"]
    lag = (now - oldest).total_seconds() if oldest else 0
    paused = conf.publishing_paused()
    return Response(
        {
            "status": "paused" if paused else ("degraded" if lag > 300 or stuck else "ok"),
            "publishing_paused": paused,
            "overdue_targets": overdue.count(),
            "oldest_overdue_seconds": int(lag),
            "stuck_in_flight": stuck,
            "last_published_at": last_publish,
            "reauth_by_provider": dict(
                Channel.objects.filter(status=ChannelStatus.NEEDS_REAUTH)
                .values_list("provider")
                .annotate(n=Count("id"))
            ),
            "failures_by_provider": {r["channel__provider"]: r["n"] for r in errors_by_provider},
            "recent_failures": [
                {
                    "target": t.id,
                    "post": t.post_id,
                    "workspace_slug": t.post.workspace.slug,
                    "organization": t.post.workspace.organization.name,
                    "workspace": t.post.workspace.name,
                    "channel": t.channel.name,
                    "provider": t.channel.provider,
                    "error": t.last_error[:500],
                    "attempts": t.attempts,
                    "at": t.updated_at,
                }
                for t in failures
            ],
        }
    )


@doc("platform", "AI usage and cost per organization", params=[query("month", "YYYY-MM, default current month")])
@api_view(["GET"])
@permission_classes([IsSuperAdmin])
def ai_usage(request):
    month = request.query_params.get("month")  # YYYY-MM
    now = timezone.now()
    try:
        year, mon = (int(x) for x in month.split("-")) if month else (now.year, now.month)
        start = now.replace(year=year, month=mon, day=1, hour=0, minute=0, second=0, microsecond=0)
    except (ValueError, AttributeError):
        raise ValidationError({"month": "Use YYYY-MM."})
    end = (start + timedelta(days=32)).replace(day=1)
    rows = (
        AIRequest.objects.filter(created_at__gte=start, created_at__lt=end)
        .values(
            "workspace__organization_id", "workspace__organization__name", "workspace__organization__ai_monthly_tokens"
        )
        .annotate(requests=Count("id"), i=Sum("input_tokens"), o=Sum("output_tokens"), **COST_AGG)
        .order_by("-o")
    )
    by_kind = (
        AIRequest.objects.filter(created_at__gte=start, created_at__lt=end)
        .values("kind")
        .annotate(requests=Count("id"), i=Sum("input_tokens"), o=Sum("output_tokens"), **COST_AGG)
    )
    return Response(
        {
            "month": f"{start:%Y-%m}",
            "pricing": {
                "input_per_mtok": conf.get("AI_PRICE_INPUT_PER_MTOK"),
                "output_per_mtok": conf.get("AI_PRICE_OUTPUT_PER_MTOK"),
                "model": conf.get("AI_MODEL"),
            },
            "organizations": [
                {
                    "organization": r["workspace__organization_id"],
                    "name": r["workspace__organization__name"],
                    "requests": r["requests"],
                    "input_tokens": r["i"] or 0,
                    "output_tokens": r["o"] or 0,
                    "quota": r["workspace__organization__ai_monthly_tokens"],
                    "cost_usd": total_cost(r),
                }
                for r in rows
            ],
            "by_kind": [
                {
                    "kind": r["kind"],
                    "requests": r["requests"],
                    "input_tokens": r["i"] or 0,
                    "output_tokens": r["o"] or 0,
                    "cost_usd": total_cost(r),
                }
                for r in by_kind
            ],
        }
    )


# --- organizations -------------------------------------------------------------


class PlatformOrganizationSerializer(serializers.ModelSerializer):
    usage = serializers.SerializerMethodField()
    owners = serializers.SerializerMethodField()
    subscription = serializers.SerializerMethodField()
    owner_email = serializers.EmailField(write_only=True, required=False)

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "status",
            "max_workspaces",
            "max_channels",
            "max_members",
            "ai_monthly_tokens",
            "notes",
            "usage",
            "owners",
            "subscription",
            "owner_email",
            "created_at",
        )
        read_only_fields = ("id", "slug", "created_at")

    def get_usage(self, obj) -> dict:
        return limits.usage(obj)

    def get_subscription(self, obj) -> dict | None:
        sub = getattr(obj, "subscription", None)
        if sub is None:
            return None
        return {
            "plan": sub.plan.name if sub.plan else None,
            "status": sub.status,
            "current_period_end": sub.current_period_end,
            "cancel_at_period_end": sub.cancel_at_period_end,
        }

    def get_owners(self, obj) -> list[str]:
        return [m.user.email for m in obj.memberships.all() if m.role == OrgRole.OWNER]


class PlatformOrganizationViewSet(viewsets.ModelViewSet):
    permission_classes = [IsSuperAdmin]
    serializer_class = PlatformOrganizationSerializer
    search_fields = ("name", "slug", "memberships__user__email")
    filterset_fields = ("status",)

    def get_queryset(self):
        return Organization.objects.prefetch_related("memberships__user").distinct()

    @transaction.atomic
    def perform_create(self, serializer):
        email = (serializer.validated_data.pop("owner_email", "") or "").lower()
        defaults = {k: v for k, v in PlatformSettings.load().default_limits().items() if k not in self.request.data}
        org = serializer.save(**defaults)
        if email:
            user = User.objects.filter(email__iexact=email).first()
            if user:
                OrgMembership.objects.create(organization=org, user=user, role=OrgRole.OWNER)
            else:
                Invitation.objects.create(
                    organization=org, email=email, org_role=OrgRole.OWNER, invited_by=self.request.user
                )

    def perform_update(self, serializer):
        serializer.validated_data.pop("owner_email", None)
        serializer.save()

    @action(detail=True, methods=["get"], url_path="detail")
    def full_detail(self, request, pk=None):
        org = self.get_object()
        workspaces = org.workspaces.annotate(
            channel_count=Count("channels", distinct=True),
            member_count=Count("memberships", distinct=True),
        )
        members = OrgMembership.objects.filter(organization=org).select_related("user")
        invitations = Invitation.objects.filter(organization=org, accepted_at__isnull=True)
        return Response(
            {
                **PlatformOrganizationSerializer(org).data,
                "workspaces": [
                    {
                        "id": w.id,
                        "name": w.name,
                        "slug": w.slug,
                        "timezone": w.timezone,
                        "archived_at": w.archived_at,
                        "channels": w.channel_count,
                        "members": w.member_count,
                        "created_at": w.created_at,
                    }
                    for w in workspaces
                ],
                "members": [
                    {
                        "id": m.id,
                        "user_id": m.user_id,
                        "email": m.user.email,
                        "name": m.user.name,
                        "role": m.role,
                        "is_active": m.user.is_active,
                    }
                    for m in members
                ],
                "invitations": [
                    {
                        "id": i.id,
                        "email": i.email,
                        "org_role": i.org_role,
                        "role": i.role,
                        "token": i.token,
                        "created_at": i.created_at,
                    }
                    for i in invitations
                ],
            }
        )

    @action(detail=True, methods=["post"])
    def workspaces(self, request, pk=None):
        org = self.get_object()
        name = (request.data.get("name") or "").strip()
        if not name:
            raise ValidationError({"name": "Required."})
        tz = request.data.get("timezone") or "UTC"
        if tz not in zoneinfo.available_timezones():
            raise ValidationError({"timezone": "Unknown time zone."})
        ws = Workspace.objects.create(organization=org, name=name, timezone=tz)
        return Response({"id": ws.id, "name": ws.name, "slug": ws.slug}, status=status.HTTP_201_CREATED)


# --- users ---------------------------------------------------------------------


class PlatformUserSerializer(serializers.ModelSerializer):
    organizations = serializers.SerializerMethodField()
    is_superadmin = serializers.BooleanField(source="is_superuser", required=False)

    class Meta:
        model = User
        fields = ("id", "email", "name", "is_active", "is_superadmin", "date_joined", "last_login", "organizations")
        read_only_fields = ("id", "email", "date_joined", "last_login")

    def get_organizations(self, obj) -> list[dict]:
        out = {}
        for m in obj.org_memberships.all():
            out[m.organization_id] = {
                "id": m.organization_id,
                "name": m.organization.name,
                "role": m.role,
                "workspaces": [],
            }
        for m in obj.memberships.all():
            org = m.workspace.organization
            entry = out.setdefault(org.id, {"id": org.id, "name": org.name, "role": None, "workspaces": []})
            entry["workspaces"].append({"id": m.workspace_id, "name": m.workspace.name, "role": m.role})
        return list(out.values())


class PlatformUserViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsSuperAdmin]
    serializer_class = PlatformUserSerializer
    search_fields = ("email", "name")
    filterset_fields = ("is_active", "is_superuser")
    ordering_fields = ("date_joined", "last_login", "email")

    def get_queryset(self):
        return User.objects.prefetch_related(
            "org_memberships__organization", "memberships__workspace__organization"
        ).order_by("-date_joined")

    def perform_update(self, serializer):
        user = serializer.instance
        if user == self.request.user and (
            serializer.validated_data.get("is_active") is False
            or serializer.validated_data.get("is_superuser") is False
        ):
            raise ValidationError("You cannot deactivate or demote yourself.")
        serializer.save()

    @action(detail=True, methods=["post"], url_path="set-password")
    def set_password(self, request, pk=None):
        """Generate a temporary password, returned once. Share it with the user out of band."""
        user = get_object_or_404(User, pk=pk)
        password = secrets.token_urlsafe(12)
        user.set_password(password)
        user.save(update_fields=["password"])
        return Response({"email": user.email, "temporary_password": password})

"""Organization plan limits. ``None`` on a limit means unlimited."""

from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError


def member_ids(org):
    from .models import Membership, OrgMembership

    ids = set(OrgMembership.objects.filter(organization=org).values_list("user_id", flat=True))
    ids |= set(Membership.objects.filter(workspace__organization=org).values_list("user_id", flat=True))
    return ids


def usage(org):
    from apps.channels.models import Channel

    return {
        "workspaces": org.workspaces.filter(archived_at__isnull=True).count(),
        "channels": Channel.objects.filter(organization=org).count(),
        "members": len(member_ids(org)),
        "ai_tokens_this_month": ai_tokens_this_month(org),
    }


def check_workspace_limit(org):
    if org.max_workspaces is not None and org.workspaces.filter(archived_at__isnull=True).count() >= org.max_workspaces:
        raise ValidationError({"detail": f"Your plan allows {org.max_workspaces} active workspaces."})


def check_channel_limit(org, adding=1):
    from apps.channels.models import Channel

    if org.max_channels is None:
        return
    current = Channel.objects.filter(organization=org).count()
    if current + adding > org.max_channels:
        raise ValidationError({"detail": f"Your plan allows {org.max_channels} channels ({current} connected)."})


def check_member_limit(org, user=None, email=None):
    if org.max_members is None:
        return
    ids = member_ids(org)
    if user is not None and user.pk in ids:
        return
    if email:
        from apps.core.models import User

        existing = User.objects.filter(email__iexact=email).values_list("pk", flat=True).first()
        if existing in ids:
            return
    if len(ids) >= org.max_members:
        raise ValidationError({"detail": f"Your plan allows {org.max_members} people in this organization."})


def ai_tokens_this_month(org):
    from apps.insights.models import AIRequest

    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    agg = AIRequest.objects.filter(workspace__organization=org, created_at__gte=start).aggregate(
        i=Sum("input_tokens"), o=Sum("output_tokens")
    )
    return (agg["i"] or 0) + (agg["o"] or 0)


def ai_quota_left(org):
    if org.ai_monthly_tokens is None:
        return None
    return max(org.ai_monthly_tokens - ai_tokens_this_month(org), 0)

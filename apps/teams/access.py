"""Who may see and publish to which channel, and who must get approval.

Rules
* Workspace admins and owners (directly or through the organization) see every channel.
* A channel assigned to no team is open to everyone in the workspace.
* A channel assigned to teams is visible only to members of those teams.
* A post needs approval when one of its channels belongs to a team with
  ``requires_approval`` and the author is not a reviewer of that team (admins never need it).
* Admins and reviewers of an approval team covering the post's channels can approve it.
"""

from django.db.models import Q

from apps.tenants.models import Role

from .models import Team, TeamMember, TeamRole


def is_unrestricted(membership):
    return membership.has_role(Role.ADMIN)


def accessible_channel_ids(membership):
    """Ids of the organization's channels this member may use in the current workspace
    (all of them, minus channels reserved to teams of this workspace the member is not in)."""
    from apps.channels.models import Channel

    ws = membership.workspace
    cache = getattr(membership, "_channel_ids", None)
    if cache is not None:
        return cache
    # Every channel of the organization is available in every workspace.
    all_ids = set(Channel.objects.filter(organization_id=ws.organization_id).values_list("id", flat=True))
    if is_unrestricted(membership):
        ids = all_ids
    else:
        restricted = set(Team.channels.through.objects.filter(team__workspace=ws).values_list("channel_id", flat=True))
        mine = set(
            Team.channels.through.objects.filter(team__workspace=ws, team__members__user=membership.user).values_list(
                "channel_id", flat=True
            )
        )
        ids = (all_ids - restricted) | (all_ids & mine)
    membership._channel_ids = ids
    return ids


def channel_q(membership, prefix=""):
    """Q filtering channels (via ``prefix``, e.g. "channel__") to those the member may use; None = all."""
    if is_unrestricted(membership):
        return None
    return Q(**{f"{prefix}id__in": accessible_channel_ids(membership)})


def filter_channels(qs, membership, prefix=""):
    q = channel_q(membership, prefix)
    return qs if q is None else qs.filter(q).distinct()


def can_use_channel(membership, channel):
    if channel.organization_id != membership.workspace.organization_id:
        return False
    return channel.id in accessible_channel_ids(membership)


def approval_teams(post):
    """Teams requiring approval that cover at least one of the post's channels."""
    channel_ids = post.targets.exclude(status="cancelled").values("channel_id")
    return Team.objects.filter(workspace=post.workspace, requires_approval=True, channels__in=channel_ids).distinct()


def needs_approval(post, membership):
    if is_unrestricted(membership):
        return False
    teams = approval_teams(post)
    if not teams.exists():
        return False
    reviewer_of = set(
        TeamMember.objects.filter(team__in=teams, user=membership.user, role=TeamRole.REVIEWER).values_list(
            "team_id", flat=True
        )
    )
    return any(t.pk not in reviewer_of for t in teams)


def can_review(post, membership):
    if is_unrestricted(membership):
        return True
    teams = approval_teams(post)
    return TeamMember.objects.filter(team__in=teams, user=membership.user, role=TeamRole.REVIEWER).exists()


def reviewable_posts_q(membership):
    """Q over Post for posts this member can review."""
    if is_unrestricted(membership):
        return Q()
    teams = TeamMember.objects.filter(
        user=membership.user,
        role=TeamRole.REVIEWER,
        team__requires_approval=True,
        team__workspace=membership.workspace,
    ).values("team_id")
    return Q(targets__channel__teams__in=teams)

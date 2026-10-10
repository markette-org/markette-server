from dataclasses import asdict

from django.db import transaction
from django.utils import timezone

from .models import Channel, ChannelAccess, ChannelStatus
from .providers import Candidate


def candidate_to_dict(c: Candidate):
    d = asdict(c)
    if c.token_expires_at:
        d["token_expires_at"] = c.token_expires_at.isoformat()
    return d


def public_candidate(d):
    return {
        k: d.get(k)
        for k in ("external_id", "name", "handle", "avatar_url", "profile_url", "provider", "followers_count")
    }


@transaction.atomic
def create_channels(conn, external_ids, user):
    from datetime import datetime

    from apps.tenants.limits import check_channel_limit

    found = {c["external_id"]: c for c in conn.candidates.get("items", [])}
    new = [
        e
        for e in external_ids
        if e in found
        and not Channel.objects.filter(
            organization=conn.workspace.organization,
            provider=found[e].get("provider") or conn.provider,
            external_id=e,
        ).exists()
    ]
    check_channel_limit(conn.workspace.organization, adding=len(new))
    created = []
    for ext in external_ids:
        c = found.get(ext)
        if c is None:
            continue
        expires = datetime.fromisoformat(c["token_expires_at"]) if c.get("token_expires_at") else None
        provider = c.get("provider") or conn.provider
        existing = Channel.objects.filter(
            organization=conn.workspace.organization, provider=provider, external_id=c["external_id"]
        ).first()
        # Reconnecting keeps settings the user chose (e.g. first-comment defaults).
        config = {**(existing.config if existing else {}), **c.get("config", {})}
        channel, _ = Channel.objects.update_or_create(
            organization=conn.workspace.organization,
            provider=provider,
            external_id=c["external_id"],
            defaults=dict(
                name=c["name"][:255],
                handle=c.get("handle", "")[:255],
                avatar_url=c.get("avatar_url", "")[:1000],
                profile_url=c.get("profile_url", "")[:1000],
                base_url=c.get("base_url", ""),
                credentials=c.get("credentials", {}),
                config=config,
                token_expires_at=expires,
                followers_count=c.get("followers_count"),
                status=ChannelStatus.ACTIVE,
                last_error="",
                connected_by=user,
            ),
        )
        # The account is organization-wide; it is marked relevant for the workspace it was connected from.
        ChannelAccess.objects.get_or_create(channel=channel, workspace=conn.workspace)
        created.append(channel)
    conn.completed_at = timezone.now()
    conn.candidates = {}  # drop the tokens of accounts the user did not pick
    conn.save(update_fields=["completed_at", "candidates", "updated_at"])
    return created


def set_relevance(channel, workspace, relevant):
    if relevant:
        ChannelAccess.objects.get_or_create(channel=channel, workspace=workspace)
    else:
        ChannelAccess.objects.filter(channel=channel, workspace=workspace).delete()


@transaction.atomic
def set_channel_workspaces(channel, workspaces):
    """Replace the set of workspaces the channel is relevant for (it stays usable everywhere)."""
    wanted = {w.id for w in workspaces}
    ChannelAccess.objects.filter(channel=channel).exclude(workspace_id__in=wanted).delete()
    for ws in workspaces:
        ChannelAccess.objects.get_or_create(channel=channel, workspace=ws)

"""Turning what happens in Markette into notifications.

``emit(workspace, event, data)`` stores one Delivery per enabled destination listening to
the event and hands it to a worker once the surrounding transaction commits. Payloads are
the same for every kind of destination (webhooks receive them verbatim); ``render`` turns
one into a short human message for email and Telegram.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Delivery, Destination, Event

log = logging.getLogger(__name__)


def app_url(path=""):
    return f"{settings.FRONTEND_URL}{path}"


# --- payload pieces ----------------------------------------------------------------------
def workspace_data(ws):
    return {"id": ws.pk, "slug": ws.slug, "name": ws.name}


def post_data(post):
    title = post.title or next((line.strip() for line in (post.body or "").splitlines() if line.strip()), "")
    return {"id": post.pk, "title": title[:200], "status": post.status, "url": app_url(f"/posts/{post.pk}")}


def channel_data(channel):
    return {"id": channel.pk, "name": channel.name, "provider": channel.provider}


def publication_data(target):
    return {
        "id": target.pk,
        "status": target.status,
        "channel": channel_data(target.channel),
        "url": target.external_url or "",
        "published_at": target.published_at.isoformat() if target.published_at else None,
        "error": target.last_error or "",
    }


# --- emitting ----------------------------------------------------------------------------
def emit(workspace, event, data):
    """Queues ``event`` for every destination of ``workspace`` that listens to it. Never raises."""
    try:
        destinations = [
            d for d in Destination.objects.filter(workspace=workspace, enabled=True) if event in (d.events or [])
        ]
        if not destinations:
            return []
        payload = {
            "event": str(event),
            "created_at": timezone.now().isoformat(),
            "workspace": workspace_data(workspace),
            "data": data,
        }
        rows = Delivery.objects.bulk_create(Delivery(destination=d, event=event, payload=payload) for d in destinations)
        ids = [r.pk for r in rows]
    except Exception:  # noqa: BLE001 - a notification problem must never break publishing
        log.exception("Could not queue %s notifications for workspace %s", event, workspace.pk)
        return []
    from .tasks import deliver

    transaction.on_commit(lambda: [deliver.delay(i) for i in ids])
    return ids


def emit_for_target(target, event):
    emit(
        target.post.workspace,
        event,
        {"post": post_data(target.post), "publication": publication_data(target)},
    )


def emit_for_post(post, event, **extra):
    emit(post.workspace, event, {"post": post_data(post), **extra})


def emit_for_channel(channel, event, **extra):
    """Channels belong to the organization: every workspace using it hears about it."""
    from apps.tenants.models import Workspace

    for ws in Workspace.objects.filter(channel_access__channel=channel, archived_at__isnull=True):
        emit(ws, event, {"channel": {**channel_data(channel), "error": channel.last_error}, **extra})


# --- human-readable form -------------------------------------------------------------------
def render(payload):
    """(subject, lines, link) for email and Telegram."""
    event, data = payload["event"], payload.get("data") or {}
    post = data.get("post") or {}
    pub = data.get("publication") or {}
    channel = (pub.get("channel") or data.get("channel") or {}).get("name", "")
    title = f"“{post['title']}”" if post.get("title") else "A post"
    link = post.get("url") or ""
    lines = []
    if event == Event.PUBLISHED:
        subject = f"Published on {channel}: {title}"
        if pub.get("url"):
            lines.append(pub["url"])
    elif event == Event.POST_PUBLISHED:
        subject = f"{title} is live on every channel"
    elif event == Event.FAILED:
        subject = f"Publishing on {channel} failed: {title}"
        lines.append(pub.get("error") or "")
    elif event == Event.UNCONFIRMED:
        subject = f"Check {channel}: {title} may or may not be live"
        lines.append(pub.get("error") or "")
    elif event == Event.RECONNECT:
        subject = f"Reconnect {channel}"
        lines.append((data.get("channel") or {}).get("error") or "Its authorization expired or was revoked.")
        link = app_url("/channels")
    elif event == Event.APPROVAL_REQUESTED:
        subject = f"{title} waits for your approval"
        if data.get("by"):
            lines.append(f"Submitted by {data['by']}.")
    elif event == Event.APPROVED:
        subject = f"{title} was approved"
        if data.get("by"):
            lines.append(f"Approved by {data['by']}.")
    elif event == Event.CHANGES_REQUESTED:
        subject = f"Changes requested on {title}"
        if data.get("by"):
            lines.append(f"{data['by']}: {data.get('comment', '')}")
    elif event == Event.INBOX:
        n = data.get("count", 0)
        subject = f"{n} new {'reply or comment' if n == 1 else 'replies and comments'} on {channel}"
        for i in data.get("items") or []:
            lines.append(f"{i.get('author', '')}: {i.get('text', '')}")
        link = app_url("/inbox")
    elif event == Event.DIGEST:
        subject = f"Today in {payload['workspace']['name']}"
        lines.extend(data.get("lines") or [])
        link = app_url("/")
    else:
        subject = data.get("message") or "Test notification from Markette"
        link = link or app_url("/settings#notifications")
    return subject, [line for line in lines if line], link

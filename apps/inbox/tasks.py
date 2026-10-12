import logging
from datetime import timedelta

from celery import shared_task
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.channels.models import Channel, ChannelStatus
from apps.channels.providers import ProviderError
from apps.publishing.models import PostTarget, TargetStatus

from .models import InboxItem, InboxState, ItemStatus

log = logging.getLogger(__name__)

RECENT = timedelta(days=14)
# On a channel's first poll, older items arrive already read: the inbox starts clean.
FRESH = timedelta(days=2)


@shared_task
def poll_inboxes():
    """Every few minutes: channels whose inbox is due are polled in the background."""
    now = timezone.now()
    due = 0
    for ch in Channel.objects.filter(status=ChannelStatus.ACTIVE, organization__status="active"):
        try:
            provider_cls = ch.get_provider().__class__
        except Exception:  # noqa: BLE001 - unknown provider
            continue
        if not provider_cls.capabilities.inbox:
            continue
        st = InboxState.objects.filter(channel=ch).first()
        if st and st.next_poll_at and st.next_poll_at > now:
            continue
        due += 1
        poll_channel.delay(ch.pk)
    return due


@shared_task
def poll_channel(channel_id):
    ch = Channel.objects.select_related("organization").filter(pk=channel_id).first()
    if ch is None:
        return 0
    provider = ch.get_provider()
    caps = provider.capabilities
    st, created = InboxState.objects.get_or_create(channel=ch)
    now = timezone.now()
    st.next_poll_at = now + timedelta(minutes=caps.inbox_interval)
    recent = list(
        PostTarget.objects.filter(channel=ch, status=TargetStatus.PUBLISHED, published_at__gte=now - RECENT)
        .exclude(external_id="")
        .order_by("-published_at")
        .values_list("external_id", flat=True)[:20]
    )
    try:
        provider.refresh_credentials()
        entries, state = provider.fetch_inbox(dict(st.state or {}), recent)
    except ProviderError as exc:
        st.last_error = _explain(provider, exc)
        st.last_polled_at = now
        if exc.retry_after:
            st.next_poll_at = now + timedelta(seconds=max(exc.retry_after, 60))
        st.save()
        return 0
    first = st.last_polled_at is None
    added = []
    targets = {
        t.external_id: t
        for t in PostTarget.objects.filter(channel=ch, external_id__in={e.post_external_id for e in entries} - {""})
    }
    for e in entries:
        if InboxItem.objects.filter(channel=ch, external_id=e.external_id).exists():
            continue
        when = e.created_at or now
        try:
            with transaction.atomic():
                item = InboxItem.objects.create(
                    channel=ch,
                    external_id=e.external_id[:500],
                    kind=e.kind,
                    text=e.text,
                    url=e.url[:1000],
                    author_name=e.author_name[:200],
                    author_handle=e.author_handle[:200],
                    author_avatar=e.author_avatar[:1000],
                    author_url=e.author_url[:1000],
                    remote_created_at=when,
                    post_external_id=e.post_external_id[:500],
                    target=targets.get(e.post_external_id),
                    context=e.context,
                    status=ItemStatus.READ if first and when < now - FRESH else ItemStatus.NEW,
                )
        except IntegrityError:
            continue
        if item.status == ItemStatus.NEW:
            added.append(item)
    st.state, st.last_polled_at, st.last_error = state, now, ""
    st.save()
    if added:
        _notify(ch, added)
    return len(added)


def _explain(provider, exc):
    if exc.status in (401, 403) or exc.auth:
        return (
            f"{provider.label} refused to share replies and comments ({exc}). Reconnect the channel to grant "
            "the permission, or check that your API access includes it."
        )[:2000]
    return str(exc)[:2000]


def _notify(channel, items):
    from apps.notifications.events import channel_data, emit
    from apps.notifications.models import Event
    from apps.tenants.models import Workspace

    sample = [
        {"author": i.author_name or i.author_handle, "text": i.text[:280], "url": i.url, "kind": i.kind}
        for i in items[:3]
    ]
    for ws in Workspace.objects.filter(channel_access__channel=channel, archived_at__isnull=True):
        emit(ws, Event.INBOX, {"channel": channel_data(channel), "count": len(items), "items": sample})


def reply(item, text, user):
    from apps.channels.providers import InboxEntry

    provider = item.channel.get_provider()
    if not provider.capabilities.inbox_reply:
        raise ProviderError(f"{provider.label} replies cannot be sent from Markette; answer on the network.")
    provider.refresh_credentials()
    entry = InboxEntry(
        external_id=item.external_id,
        kind=item.kind,
        text=item.text,
        url=item.url,
        author_handle=item.author_handle,
        post_external_id=item.post_external_id,
        context=item.context or {},
    )
    result = provider.reply_inbox(entry, text)
    item.reply_text = text
    item.reply_url = result.url or ""
    item.replied_at = timezone.now()
    item.replied_by = user
    item.status = ItemStatus.DONE
    item.save(update_fields=["reply_text", "reply_url", "replied_at", "replied_by", "status"])
    return item

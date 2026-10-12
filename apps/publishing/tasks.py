"""The publishing pipeline.

dispatch_due_targets (beat, every few seconds)
    → claims due targets with SELECT … FOR UPDATE SKIP LOCKED
    → publish_target.delay(id) for each
publish_target
    → refresh credentials → validate → provider.publish
    → success: store id/url, schedule first metrics pull, release the publications
      that follow this one (they link to its URL)
    → transient error: back off and let the dispatcher retry
    → auth error: flag the channel for reconnection
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.channels.models import ChannelStatus
from apps.channels.providers import Content, MediaItem, ProviderError
from apps.notifications.events import emit_for_post, emit_for_target
from apps.notifications.models import Event
from apps.platform import conf

from .models import PostStatus, PostTarget, TargetStatus

log = logging.getLogger(__name__)
DISPATCH_BATCH = 200
STUCK_AFTER = timedelta(minutes=20)
# Clock skew allowed when looking for a post an earlier attempt may have created.
LOOKUP_SKEW = timedelta(minutes=5)


# Stands in for the URL of a publication that is not live yet, so lengths are checked realistically.
SAMPLE_URL = "https://example.org/" + "x" * 40


def fill_follow(text, title, link, target: PostTarget):
    """Puts the followed publication's URL (and title) into a follower's text.

    ``{url}`` and ``{title}`` (or the first line of its text) are replaced; without ``{url}`` the URL is added at the end,
    because most networks only link what is in the text.
    """
    src = target.follows
    url = src.external_url or SAMPLE_URL
    # Untitled publications (most social posts) lend the first line of their text instead.
    src_title = src.effective_title or next((ln.strip() for ln in src.effective_body.splitlines() if ln.strip()), "")
    src_title = src_title[:100]
    if "{url}" in text or "{title}" in text:
        text = text.replace("{url}", url).replace("{title}", src_title)
    if url not in text:
        text = f"{text.rstrip()}\n\n{url}" if text.strip() else url
    title = title.replace("{url}", url).replace("{title}", src_title)
    return text, title, link or url


def with_backlinks(text, backlinks, target):
    """Adds "Discuss this post on …" with the announcements' links (Markdown on blogs)."""
    kind = target.channel.get_provider().capabilities.kind
    prefix = (target.options or {}).get("backlinks_text") or "Discuss this post on"
    if kind in ("blog", "newsletter"):
        links = " · ".join(f"[{b['label']}]({b['url']})" for b in backlinks)
        return f"{text.rstrip()}\n\n---\n\n{prefix} {links}."
    links = " · ".join(f"{b['label']}: {b['url']}" for b in backlinks)
    return f"{text.rstrip()}\n\n{prefix} {links}"


def build_content(target: PostTarget) -> Content:
    media = []
    for a in target.effective_media():
        media.append(
            MediaItem(
                path=Path(a.file.path),
                url=a.public_url,
                mime=a.mime,
                alt=a.alt_text,
                width=a.width,
                height=a.height,
                size=a.size,
                filename=a.original_name or Path(a.file.name).name,
            )
        )
    post = target.post
    text, title, link = target.effective_body, target.effective_title, post.link
    if target.follows_id:
        text, title, link = fill_follow(text, title, link, target)
    if backlinks := (target.extra or {}).get("backlinks"):
        text = with_backlinks(text, backlinks, target)
    from apps.links.tracking import apply as track_links

    provider = target.channel.get_provider()
    text, link = track_links(target, text, link, provider_key=provider.key, kind=provider.capabilities.kind)
    return Content(
        text=text,
        title=title,
        link=link,
        media=media,
        options=target.options or {},
        idempotency_key=f"markette-target-{target.pk}",
        tags=post.tags or [],
    )


def backoff(attempts, retry_after=None):
    if retry_after:
        return timedelta(seconds=min(max(retry_after, 30), 6 * 3600))
    return timedelta(seconds=min(60 * 2 ** (attempts - 1), 3600))


@shared_task
def dispatch_due_targets():
    if conf.publishing_paused():
        return 0
    now = timezone.now()
    with transaction.atomic():
        due = list(
            PostTarget.objects.select_for_update(skip_locked=True)
            .filter(
                status=TargetStatus.SCHEDULED,
                scheduled_at__lte=now,
                channel__status=ChannelStatus.ACTIVE,
                # Suspended organizations and archived workspaces publish nothing.
                post__workspace__archived_at__isnull=True,
                post__workspace__organization__status="active",
            )
            .exclude(next_attempt_at__gt=now)
            .order_by("scheduled_at")[:DISPATCH_BATCH]
        )
        ids = [t.pk for t in due]
        if ids:
            PostTarget.objects.filter(pk__in=ids).update(status=TargetStatus.QUEUED, queued_at=now)
            transaction.on_commit(lambda: [publish_target.delay(i) for i in ids])
    # Targets whose channel lost its authorization fail visibly instead of waiting forever.
    stale = PostTarget.objects.filter(
        status=TargetStatus.SCHEDULED,
        scheduled_at__lte=now - timedelta(minutes=5),
    ).exclude(channel__status=ChannelStatus.ACTIVE)
    for t in stale.select_related("channel", "post"):
        t.status = TargetStatus.FAILED
        t.last_error = f"Channel {t.channel.name} needs to be reconnected."
        t.save(update_fields=["status", "last_error", "updated_at"])
        t.post.refresh_status()
        emit_for_target(t, Event.FAILED)
    # Followers whose publication was deleted would wait forever.
    orphans = PostTarget.objects.filter(status=TargetStatus.SCHEDULED, scheduled_at__isnull=True, follows__isnull=True)
    for t in orphans.select_related("post"):
        t.status = TargetStatus.FAILED
        t.last_error = "The publication it was waiting for was deleted."
        t.save(update_fields=["status", "last_error", "updated_at"])
        t.post.refresh_status()
        emit_for_target(t, Event.FAILED)
    return len(ids)


@shared_task(acks_late=True)
def publish_target(target_id):
    with transaction.atomic():
        target = (
            PostTarget.objects.select_for_update()
            .select_related("channel", "post__workspace")
            .filter(pk=target_id)
            .first()
        )
        if target is None or target.status != TargetStatus.QUEUED:
            return "skipped"
        if target.follows_id and target.follows.status != TargetStatus.PUBLISHED:
            # Retried by hand before the publication it follows: wait for it again.
            target.status = TargetStatus.SCHEDULED
            target.scheduled_at = None
            target.save(update_fields=["status", "scheduled_at", "updated_at"])
            return "waiting"
        target.status = TargetStatus.PUBLISHING
        target.attempts += 1
        target.save(update_fields=["status", "attempts", "updated_at"])
    target.post.refresh_status()

    channel = target.channel
    provider = channel.get_provider()
    started, sending = timezone.now(), False
    try:
        provider.refresh_credentials()
        content = build_content(target)
        problems = provider.validate(content)
        if problems:
            raise ProviderError(" ".join(problems))
        content, first_comment = provider.prepare_first_comment(content)
        parts = provider.thread_parts(content)
        content = parts[0]
        result = _earlier_copy(target, provider, content)
        if result is None:
            sending = True  # only a lost answer from here on may have created the post
            result = provider.publish(content)
    except CannotCheck as exc:
        return mark_unconfirmed(target, str(exc))
    except ProviderError as exc:
        return _handle_failure(target, exc, provider, started if sending else None)
    except Exception as exc:  # noqa: BLE001 - a bug must not leave the target stuck in "publishing"
        log.exception("Unexpected error publishing target %s", target_id)
        return _handle_failure(target, ProviderError(f"Internal error: {exc}"))

    now = timezone.now()
    target.status = TargetStatus.PUBLISHED
    target.published_at = now
    target.external_id = result.external_id
    target.external_url = result.url[:1000]
    target.last_error = ""
    target.next_attempt_at = None
    target.next_metrics_at = now + timedelta(hours=1) if provider.capabilities.has_metrics else None
    target.extra = {**(target.extra or {}), **{k: v for k, v in result.extra.items() if v is not None}}
    target.extra.pop("maybe_live_since", None)
    if result.extra.get("note"):
        # Published, but with a caveat worth showing (e.g. YouTube kept the video private).
        target.last_error = f"Published, but {result.extra['note'][0].lower()}{result.extra['note'][1:]}"
    if first_comment:
        # The post is live: a failing comment is reported but never retried (no duplicate posts).
        try:
            target.extra["first_comment_id"] = provider.post_first_comment(result.external_id, first_comment)
        except ProviderError as exc:
            target.last_error = f"Published, but the first comment could not be posted: {exc}"[:4000]
    if len(parts) > 1:
        target.extra["thread"] = {"total": len(parts), "posts": [_thread_entry(result)]}
    target.save()
    if len(parts) > 1:
        continue_thread(target, provider, parts)
    was = target.post.status
    if target.post.refresh_status() == PostStatus.PUBLISHED and was != PostStatus.PUBLISHED:
        emit_for_post(target.post, Event.POST_PUBLISHED)
    emit_for_target(target, Event.PUBLISHED)
    log.info("Published target %s on %s: %s", target.pk, channel.provider, result.url)
    from .scheduling import release_followers

    with transaction.atomic():
        release_followers(target)
    if target.follows_id and (target.follows.options or {}).get("backlinks"):
        source_id = target.follows_id
        transaction.on_commit(lambda: refresh_backlinks.delay(source_id))
    return "published"


def update_remote(target):
    """Pushes the publication's current text and title to the network (first post of a thread)."""
    provider = target.channel.get_provider()
    if not provider.capabilities.supports_edit:
        raise ProviderError(f"{provider.label} posts cannot be edited from Markette.")
    provider.refresh_credentials()
    content = build_content(target)
    problems = provider.validate(content)
    if problems:
        raise ProviderError(" ".join(problems))
    content, _ = provider.prepare_first_comment(content)
    content = provider.thread_parts(content)[0]
    stored = {**(target.extra or {}), "url": target.external_url}
    changes = provider.update(target.external_id, content, stored) or {}
    if changes.get("url"):
        target.external_url = changes.pop("url")[:1000]
    target.extra = {**(target.extra or {}), **changes, "edited_at": timezone.now().isoformat()}
    target.save(update_fields=["extra", "external_url", "updated_at"])


@shared_task
def refresh_backlinks(source_id):
    """Adds the announcements' links to the publication they announce, once they are live."""
    source = PostTarget.objects.select_related("channel", "post__workspace").filter(pk=source_id).first()
    if source is None or source.status != TargetStatus.PUBLISHED:
        return "skipped"
    followers = list(
        source.followers.filter(status=TargetStatus.PUBLISHED)
        .exclude(external_url="")
        .select_related("channel")
        .order_by("published_at")
    )
    providers = [f.channel.provider for f in followers]
    backlinks = [
        {
            # The network's name, or the channel's when two announcements share a network.
            "label": f.channel.name if providers.count(f.channel.provider) > 1 else f.channel.get_provider().label,
            "url": f.external_url,
        }
        for f in followers
    ]
    if backlinks == (source.extra or {}).get("backlinks"):
        return "unchanged"
    previous = (source.extra or {}).get("backlinks")
    source.extra = {**(source.extra or {}), "backlinks": backlinks}
    try:
        update_remote(source)
    except ProviderError as exc:
        source.extra["backlinks"] = previous
        source.last_error = f"Published, but the links to its announcements could not be added: {exc}"[:4000]
        source.save(update_fields=["extra", "last_error", "updated_at"])
        return "failed"
    return "updated"


def _thread_entry(result):
    return {"id": result.external_id, "url": result.url, "cid": result.extra.get("cid")}


def continue_thread(target, provider=None, parts=None):
    """Posts the thread's remaining parts as replies, saving progress after each one.

    A failure stops the thread (the start is live, so nothing is retried automatically); the
    post page offers to continue it.
    """
    from apps.channels.providers.base import PublishResult

    provider = provider or target.channel.get_provider()
    if parts is None:
        content, _ = provider.prepare_first_comment(build_content(target))
        parts = provider.thread_parts(content)
    thread = target.extra["thread"]
    done = thread["posts"]

    def as_result(entry):
        return PublishResult(external_id=entry["id"], url=entry.get("url", ""), extra={"cid": entry.get("cid")})

    root = as_result(done[0])
    for i in range(len(done), len(parts)):
        try:
            reply = provider.publish_reply(parts[i], parent=as_result(done[-1]), root=root)
        except ProviderError as exc:
            maybe = " It may have been posted: check before continuing." if exc.uncertain else ""
            target.last_error = (
                f"Published, but the thread stopped after part {len(done)} of {len(parts)}: {exc}.{maybe}"[:4000]
            )
            target.save(update_fields=["last_error", "extra", "updated_at"])
            return False
        done.append(_thread_entry(reply))
        target.save(update_fields=["extra", "updated_at"])
    if target.last_error.startswith("Published, but the thread stopped"):
        target.last_error = ""
        target.save(update_fields=["last_error", "updated_at"])
    return True


class CannotCheck(Exception):
    """The network could not say whether an earlier attempt landed (e.g. missing read permission)."""


def _earlier_copy(target, provider, content):
    """After a lost answer, the post an earlier attempt created, so it is not published twice."""
    since = (target.extra or {}).get("maybe_live_since")
    if not since or provider.duplicate_guard != "lookup":
        return None
    try:
        found = provider.find_published(content, datetime.fromisoformat(since))
    except ProviderError as exc:
        if exc.retryable:
            raise  # the network is busy: look again later
        raise CannotCheck(f"{target.channel.name} could not be checked for the earlier attempt ({exc}).") from exc
    if found and (
        PostTarget.objects.filter(channel=target.channel, external_id=found.external_id).exclude(pk=target.pk).exists()
    ):
        return None  # that post belongs to another publication
    if found:
        log.info("Target %s was already live on %s: %s", target.pk, target.channel.provider, found.url)
    return found


def mark_unconfirmed(target, reason):
    """The post may be live: hold it, and its followers, until a person checks the network."""
    target.status = TargetStatus.UNCONFIRMED
    target.next_attempt_at = None
    target.last_error = (
        f"{reason} It may be live: check {target.channel.name}, then mark it as published or publish it again."
    )[:4000]
    target.save(update_fields=["status", "last_error", "next_attempt_at", "extra", "updated_at"])
    target.post.refresh_status()
    from .scheduling import note_followers

    note_followers(target, f"Waiting for {target.channel.name}, whose publication needs checking.")
    emit_for_target(target, Event.UNCONFIRMED)
    log.warning("Publishing target %s is unconfirmed: %s", target.pk, reason)
    return target.status


def _handle_failure(target, exc: ProviderError, provider=None, started=None):
    if exc.uncertain and started:
        extra = target.extra or {}
        # Keep the earliest moment the post may have been created.
        extra.setdefault("maybe_live_since", (started - LOOKUP_SKEW).isoformat())
        target.extra = extra
        if not (provider and provider.duplicate_guard):
            return mark_unconfirmed(target, f"{target.channel.name} did not answer in time ({exc}).")
    target.last_error = str(exc)[:4000]
    if exc.auth:
        target.channel.mark_needs_reauth(str(exc))
        target.status = TargetStatus.FAILED
    elif exc.retryable and target.attempts < conf.get("PUBLISH_MAX_ATTEMPTS"):
        target.status = TargetStatus.SCHEDULED
        target.next_attempt_at = timezone.now() + backoff(target.attempts, exc.retry_after)
    else:
        target.status = TargetStatus.FAILED
    target.save(update_fields=["status", "last_error", "next_attempt_at", "extra", "updated_at"])
    target.post.refresh_status()
    if target.status == TargetStatus.FAILED:
        from .scheduling import note_followers

        emit_for_target(target, Event.FAILED)
        note_followers(target, f"Waiting for {target.channel.name}, which failed. Retry it to continue.")
    log.warning("Publishing target %s failed (%s): %s", target.pk, target.status, exc)
    return target.status


@shared_task
def requeue_stuck_targets():
    cutoff = timezone.now() - STUCK_AFTER
    # Never handed to a worker (broker hiccup): safe to retry.
    requeued = PostTarget.objects.filter(status=TargetStatus.QUEUED, queued_at__lt=cutoff).update(
        status=TargetStatus.SCHEDULED,
        updated_at=timezone.now(),
    )
    # A worker died mid-call: the post may or may not be live. Networks that can tell (or drop
    # repeats) are retried; for the others a person checks, so nothing is published twice.
    stuck = PostTarget.objects.filter(status=TargetStatus.PUBLISHING, updated_at__lt=cutoff)
    for t in stuck.select_related("post", "channel"):
        t.extra = {"maybe_live_since": (t.updated_at - LOOKUP_SKEW).isoformat(), **(t.extra or {})}
        if t.channel.get_provider().duplicate_guard:
            t.status = TargetStatus.SCHEDULED
            t.next_attempt_at = None
            t.save(update_fields=["status", "next_attempt_at", "extra", "updated_at"])
            t.post.refresh_status()
            requeued += 1
        else:
            mark_unconfirmed(t, "Publishing was interrupted.")
    return requeued

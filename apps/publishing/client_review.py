"""Review links: a client without an account approves a post, or asks for changes.

Approving through a link counts like a reviewer's approval: a post waiting for review gets
its stored schedule applied. Asking for changes stops it: scheduled channels go back to
draft until someone schedules the post again.
"""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.notifications.events import emit_for_post
from apps.notifications.models import Event

from . import approval
from .models import ApprovalStatus, PostReview, ReviewAction, TargetStatus


def preview(link):
    post = link.post
    tz = post.workspace.tz
    targets = post.targets.exclude(status=TargetStatus.CANCELLED).select_related("channel")
    media = [{"url": m.public_url, "mime": m.mime, "alt": m.alt_text} for m in post.ordered_media()]
    return {
        "workspace": post.workspace.name,
        "label": link.label,
        "expires_at": link.expires_at,
        "post": {
            "title": post.title,
            "body": post.body,
            "link": post.link,
            "approval_status": post.approval_status,
            "media": media,
        },
        "channels": [
            {
                "name": t.channel.name,
                "provider": t.channel.provider,
                "provider_label": t.channel.get_provider().label,
                "title": t.effective_title,
                "body": t.effective_body,
                "status": t.status,
                "when": (t.scheduled_at or t.published_at).astimezone(tz).isoformat()
                if (t.scheduled_at or t.published_at)
                else None,
            }
            for t in targets
        ],
        "timezone": post.workspace.timezone,
        "reviews": [
            {
                "action": r.action,
                "comment": r.comment,
                "by": r.reviewer_name or ((r.user.name or "A team member") if r.user else ""),
                "created_at": r.created_at,
            }
            for r in post.reviews.select_related("user").exclude(action=ReviewAction.RESET)
        ],
    }


@transaction.atomic
def decide(link, decision, name, comment):
    post = link.post
    name = (name or "").strip()[:120] or link.label or "Client"
    comment = (comment or "").strip()[:4000]
    by = f"{name} (review link)"
    if decision == "approve":
        was_pending = post.approval_status == ApprovalStatus.PENDING
        post.approval_status = ApprovalStatus.APPROVED
        post.reviewed_at = timezone.now()
        post.save(update_fields=["approval_status", "reviewed_at", "updated_at"])
        PostReview.objects.create(
            post=post, action=ReviewAction.APPROVED, comment=comment, reviewer_name=name, review_link=link
        )
        emit_for_post(post, Event.APPROVED, by=by, comment=comment)
        stored = post.pending_schedule
        if was_pending and stored:
            post.pending_schedule = None
            post.save(update_fields=["pending_schedule"])
            try:
                with transaction.atomic():
                    approval.apply_schedule(post, approval._decode(stored))
            except ValidationError:
                pass  # approved; the requested time no longer works, the team reschedules
        return
    if decision == "changes":
        if not comment:
            raise ValidationError({"comment": "Say what should change."})
        post.approval_status = ApprovalStatus.CHANGES_REQUESTED
        post.reviewed_at = timezone.now()
        post.save(update_fields=["approval_status", "reviewed_at", "updated_at"])
        post.targets.filter(status=TargetStatus.SCHEDULED).update(
            status=TargetStatus.DRAFT, scheduled_at=None, updated_at=timezone.now()
        )
        post.refresh_status()
        PostReview.objects.create(
            post=post, action=ReviewAction.CHANGES_REQUESTED, comment=comment, reviewer_name=name, review_link=link
        )
        emit_for_post(post, Event.CHANGES_REQUESTED, by=by, comment=comment)
        return
    raise ValidationError({"decision": "Either approve or changes."})

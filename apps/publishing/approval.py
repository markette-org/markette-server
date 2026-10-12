"""Team approval workflow.

author schedules ──needs approval?──► pending (request stored, nothing scheduled)
reviewer approves ──► approved ──► stored request is applied (targets scheduled)
reviewer requests changes ──► changes_requested ──► author edits, schedules again ──► pending
author edits an approved post ──► approval reset, scheduled targets back to draft
"""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.notifications.events import emit_for_post
from apps.notifications.models import Event
from apps.teams.access import can_review, needs_approval

from . import scheduling
from .models import ApprovalStatus, PostReview, ReviewAction, TargetStatus


def _name(user):
    return user.name or user.email


def _encode(request):
    out = {"mode": request["mode"]}
    for key in ("at", "window_start", "window_end"):
        if request.get(key):
            out[key] = request[key].isoformat()
    if request.get("per_target"):
        out["per_target"] = {str(k): v.isoformat() for k, v in request["per_target"].items()}
    return out


def _decode(stored):
    req = {"mode": stored["mode"]}
    for key in ("at", "window_start", "window_end"):
        req[key] = parse_datetime(stored[key]) if stored.get(key) else None
    req["per_target"] = {
        (int(k) if k.isdigit() else k): parse_datetime(v) for k, v in (stored.get("per_target") or {}).items()
    }
    return req


def apply_schedule(post, request):
    return scheduling.schedule_post(
        post,
        request["mode"],
        request.get("at"),
        request.get("window_start"),
        request.get("window_end"),
        request.get("per_target"),
    )


@transaction.atomic
def schedule_or_submit(post, membership, request, comment=""):
    """Schedule now if the user may, else park the request for review. Returns True when submitted."""
    if post.approval_status == ApprovalStatus.APPROVED or not needs_approval(post, membership):
        apply_schedule(post, request)
        return False
    post.approval_status = ApprovalStatus.PENDING
    post.pending_schedule = _encode(request)
    post.submitted_by = membership.user
    post.submitted_at = timezone.now()
    post.save(update_fields=["approval_status", "pending_schedule", "submitted_by", "submitted_at", "updated_at"])
    PostReview.objects.create(post=post, user=membership.user, action=ReviewAction.SUBMITTED, comment=comment)
    emit_for_post(post, Event.APPROVAL_REQUESTED, by=_name(membership.user), comment=comment)
    return True


@transaction.atomic
def approve(post, membership, comment=""):
    if not can_review(post, membership):
        raise PermissionDenied("Only a reviewer of this post's team can approve it.")
    if post.approval_status != ApprovalStatus.PENDING:
        raise ValidationError("This post is not waiting for review.")
    post.approval_status = ApprovalStatus.APPROVED
    post.reviewed_by = membership.user
    post.reviewed_at = timezone.now()
    post.save(update_fields=["approval_status", "reviewed_by", "reviewed_at", "updated_at"])
    PostReview.objects.create(post=post, user=membership.user, action=ReviewAction.APPROVED, comment=comment)
    emit_for_post(post, Event.APPROVED, by=_name(membership.user), comment=comment)
    stored = post.pending_schedule
    if stored:
        post.pending_schedule = None
        post.save(update_fields=["pending_schedule"])
        try:
            with transaction.atomic():
                apply_schedule(post, _decode(stored))
        except ValidationError as exc:
            # Approved, but the requested time no longer works (e.g. it passed while waiting).
            return {"scheduled": False, "detail": exc.detail}
        return {"scheduled": True}
    return {"scheduled": False}


@transaction.atomic
def request_changes(post, membership, comment):
    if not can_review(post, membership):
        raise PermissionDenied("Only a reviewer of this post's team can request changes.")
    if post.approval_status != ApprovalStatus.PENDING:
        raise ValidationError("This post is not waiting for review.")
    if not comment.strip():
        raise ValidationError({"comment": "Tell the author what to change."})
    post.approval_status = ApprovalStatus.CHANGES_REQUESTED
    post.reviewed_by = membership.user
    post.reviewed_at = timezone.now()
    post.save(update_fields=["approval_status", "reviewed_by", "reviewed_at", "updated_at"])
    PostReview.objects.create(post=post, user=membership.user, action=ReviewAction.CHANGES_REQUESTED, comment=comment)
    emit_for_post(post, Event.CHANGES_REQUESTED, by=_name(membership.user), comment=comment)


def reset_after_edit(post, membership):
    """An approved post edited by someone who needs approval goes back to draft."""
    if post.approval_status != ApprovalStatus.APPROVED or not needs_approval(post, membership):
        return
    post.approval_status = ApprovalStatus.NONE
    post.save(update_fields=["approval_status", "updated_at"])
    post.targets.filter(status=TargetStatus.SCHEDULED).update(
        status=TargetStatus.DRAFT, scheduled_at=None, updated_at=timezone.now()
    )
    post.refresh_status()
    PostReview.objects.create(post=post, user=membership.user, action=ReviewAction.RESET)


def guard_direct_scheduling(post, membership):
    """Calendar moves must not bypass review."""
    if post.approval_status != ApprovalStatus.APPROVED and needs_approval(post, membership):
        raise ValidationError("This post needs a reviewer's approval before it can be scheduled.")

import mimetypes
import re

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from PIL import Image
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.channels.providers import ProviderError
from apps.teams import access
from apps.tenants.models import Role
from apps.tenants.tenancy import WorkspaceScopedMixin

from . import approval, scheduling
from .models import ApprovalStatus, Campaign, MediaAsset, Post, PostingSlot, PostMedia, PostTarget, TargetStatus
from .serializers import (
    CalendarItemSerializer,
    CampaignSerializer,
    MediaAssetSerializer,
    PostSerializer,
    ScheduleSerializer,
    SlotSerializer,
    TargetSerializer,
)
from .tasks import build_content

URL_RE = re.compile(r"^https?://\S+$")

ALLOWED_MEDIA = ("image/jpeg", "image/png", "image/gif", "image/webp", "video/mp4", "video/quicktime")


class MediaViewSet(
    WorkspaceScopedMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    queryset = MediaAsset.objects.all()
    serializer_class = MediaAssetSerializer
    parser_classes = (MultiPartParser, FormParser, JSONParser)

    def create(self, request):
        upload = request.FILES.get("file")
        if not upload:
            raise ValidationError({"file": "No file uploaded."})
        if upload.size > settings.MAX_MEDIA_UPLOAD_BYTES:
            raise ValidationError({"file": "File too large."})
        mime = upload.content_type or mimetypes.guess_type(upload.name)[0] or ""
        if mime not in ALLOWED_MEDIA:
            raise ValidationError({"file": f"Unsupported type {mime}. Use JPEG, PNG, GIF, WebP, MP4 or MOV."})
        width = height = None
        if mime.startswith("image/"):
            try:
                with Image.open(upload) as img:
                    img.verify()
                upload.seek(0)
                with Image.open(upload) as img:
                    width, height = img.size
                upload.seek(0)
            except Exception as exc:
                raise ValidationError({"file": "This image could not be read."}) from exc
        asset = MediaAsset.objects.create(
            workspace=self.workspace,
            file=upload,
            original_name=upload.name[:255],
            mime=mime,
            size=upload.size,
            width=width,
            height=height,
            alt_text=request.data.get("alt_text", ""),
            uploaded_by=request.user,
        )
        return Response(MediaAssetSerializer(asset).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        if PostMedia.objects.filter(asset=instance, post__targets__status=TargetStatus.SCHEDULED).exists():
            raise ValidationError("This media is used by a scheduled post.")
        instance.file.delete(save=False)
        instance.delete()


class CampaignViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    queryset = Campaign.objects.annotate(post_count=Count("posts"))
    serializer_class = CampaignSerializer


class SlotViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    queryset = PostingSlot.objects.all()
    serializer_class = SlotSerializer
    filterset_fields = ("channel", "team", "weekday", "active")
    pagination_class = None

    def get_queryset(self):
        qs = super().get_queryset()
        q = access.channel_q(self.membership, "channel__")
        return qs if q is None else qs.filter(Q(channel__isnull=True) | q).distinct()

    @action(detail=False, methods=["post"])
    def preset(self, request):
        """Replace the workspace-wide queue with a sensible starting set."""
        times = request.data.get("times") or ["09:15", "12:30", "17:45"]
        days = request.data.get("weekdays") or [0, 1, 2, 3, 4]
        PostingSlot.objects.filter(workspace=self.workspace, channel__isnull=True, team__isnull=True).delete()
        slots = [PostingSlot(workspace=self.workspace, weekday=d, time=t) for d in days for t in times]
        PostingSlot.objects.bulk_create(slots)
        return Response(SlotSerializer(PostingSlot.objects.filter(workspace=self.workspace), many=True).data)


class PostViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    queryset = Post.objects.select_related("author").prefetch_related(
        "postmedia_set__asset",
        "targets__channel",
        "targets__follows__channel",
        "targets__follows__post",
    )
    serializer_class = PostSerializer
    filterset_fields = ("campaign",)
    search_fields = ("title", "body")
    ordering_fields = ("created_at", "updated_at")

    def get_queryset(self):
        qs = super().get_queryset()
        q = access.channel_q(self.membership, "targets__channel__")
        if q is not None:
            qs = qs.filter(q | Q(author=self.request.user) | Q(targets__isnull=True)).distinct()
        params = self.request.query_params
        if approval_param := params.get("approval"):
            values = ["" if v == "none" else v for v in approval_param.split(",")]
            qs = qs.filter(approval_status__in=values)
        if params.get("review") == "mine":
            qs = qs.filter(Q(approval_status=ApprovalStatus.PENDING) & access.reviewable_posts_q(self.membership))
            qs = qs.exclude(submitted_by=self.request.user).distinct()
        if channel := self.request.query_params.get("channel"):
            qs = qs.filter(targets__channel_id=channel).distinct()
        if status_param := self.request.query_params.get("status"):
            # Comma-separated: ?status=failed,partial
            qs = qs.filter(status__in=status_param.split(","))
        return qs

    def perform_create(self, serializer):
        serializer.save(workspace=self.workspace, author=self.request.user)

    def perform_update(self, serializer):
        post = serializer.save()
        approval.reset_after_edit(post, self.membership)

    def perform_destroy(self, post):
        if post.targets.filter(status__in=(TargetStatus.QUEUED, TargetStatus.PUBLISHING)).exists():
            raise ValidationError("This post is being published right now.")
        post.delete()

    def _fresh(self, post):
        return Response(PostSerializer(self.get_queryset().get(pk=post.pk), context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def schedule(self, request, pk=None):
        post = self.get_object()
        s = ScheduleSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        problems = _validate_post(post)
        if problems:
            raise ValidationError({"targets": problems})
        submitted = approval.schedule_or_submit(post, self.membership, d, request.data.get("comment", ""))
        response = self._fresh(post)
        if submitted:
            response.status_code = status.HTTP_202_ACCEPTED
        return response

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        post = self.get_object()
        outcome = approval.approve(post, self.membership, request.data.get("comment", ""))
        data = self._fresh(post).data
        data["approval_outcome"] = outcome
        return Response(data)

    @action(detail=True, methods=["post"], url_path="request-changes")
    def request_changes(self, request, pk=None):
        post = self.get_object()
        approval.request_changes(post, self.membership, request.data.get("comment", ""))
        return self._fresh(post)

    @action(detail=True, methods=["get"])
    def reviews(self, request, pk=None):
        post = self.get_object()
        rows = post.reviews.select_related("user")
        return Response(
            [
                {
                    "id": r.id,
                    "action": r.action,
                    "comment": r.comment,
                    "created_at": r.created_at,
                    "user": (r.user.name or r.user.email) if r.user else None,
                }
                for r in rows
            ]
        )

    @action(detail=True, methods=["post"])
    def unschedule(self, request, pk=None):
        post = self.get_object()
        scheduling.unschedule_post(post)
        return self._fresh(post)

    @action(detail=True, methods=["get"])
    def validate(self, request, pk=None):
        return Response(_validate_post(self.get_object()))

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        post = self.get_object()
        failed = post.targets.filter(status=TargetStatus.FAILED, channel__status="active")
        # Followers whose publication is not live yet go back to waiting for it.
        waiting = failed.filter(follows__isnull=False).exclude(follows__status=TargetStatus.PUBLISHED)
        waiting.update(
            status=TargetStatus.SCHEDULED, scheduled_at=None, next_attempt_at=None, attempts=0, last_error=""
        )
        failed.update(
            status=TargetStatus.SCHEDULED,
            scheduled_at=timezone.now(),
            next_attempt_at=None,
            attempts=0,
            last_error="",
        )
        post.refresh_status()
        from .tasks import dispatch_due_targets

        dispatch_due_targets.delay()
        return self._fresh(post)

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        src = self.get_object()
        copy = Post.objects.create(
            workspace=src.workspace,
            author=request.user,
            campaign=src.campaign,
            title=src.title,
            body=src.body,
            link=src.link,
            tags=src.tags,
            notes=src.notes,
        )
        PostMedia.objects.bulk_create(
            [PostMedia(post=copy, asset_id=pm.asset_id, position=pm.position) for pm in src.postmedia_set.all()]
        )
        PostTarget.objects.bulk_create(
            [
                PostTarget(
                    post=copy,
                    channel_id=t.channel_id,
                    body=t.body,
                    title=t.title,
                    media_ids=t.media_ids,
                    options=t.options,
                    # A follower of another post keeps following it; within the post it is re-linked below.
                    follows_id=t.follows_id if t.follows and t.follows.post_id != src.pk else None,
                    follow_mode=t.follow_mode,
                    follow_delay=t.follow_delay,
                )
                for t in src.targets.select_related("follows")
            ]
        )
        by_channel = {t.channel_id: t for t in copy.targets.all()}
        for t in src.targets.select_related("follows"):
            if t.follows and t.follows.post_id == src.pk and t.follows.channel_id in by_channel:
                by_channel[t.channel_id].follows = by_channel[t.follows.channel_id]
                by_channel[t.channel_id].save(update_fields=["follows"])
        return Response(
            PostSerializer(self.get_queryset().get(pk=copy.pk), context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


def _validate_post(post):
    """Per-channel problems that would make publishing fail, keyed by target id."""
    problems = {}
    for t in post.targets.select_related("channel"):
        if t.status in (TargetStatus.PUBLISHED, TargetStatus.CANCELLED):
            continue
        provider = t.channel.get_provider()
        issues = provider.validate(build_content(t))
        if t.channel.status != "active":
            issues.append(f"{t.channel.name} needs to be reconnected.")
        if issues:
            problems[t.pk] = issues
    return problems


class TargetViewSet(WorkspaceScopedMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    queryset = PostTarget.objects.select_related("channel", "post")
    serializer_class = TargetSerializer
    workspace_field = "post__workspace"

    def get_permissions(self):
        if self.action == "delete_remote":
            self.required_role = Role.ADMIN
        return super().get_permissions()

    def get_queryset(self):
        return access.filter_channels(super().get_queryset(), self.membership, "channel__")

    def perform_update(self, serializer):
        if serializer.instance.status in (
            TargetStatus.PUBLISHED,
            TargetStatus.PUBLISHING,
            TargetStatus.QUEUED,
            TargetStatus.UNCONFIRMED,
        ):
            raise ValidationError("A publication that is live or in flight cannot be edited.")
        serializer.save()

    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        when = parse_datetime(request.data.get("scheduled_at") or "")
        if when is None:
            raise ValidationError({"scheduled_at": "Invalid date."})
        target = self.get_object()
        approval.guard_direct_scheduling(target.post, self.membership)
        target = scheduling.reschedule_target(target, when)
        return Response(TargetSerializer(target, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        target = self.get_object()
        if target.status not in (
            TargetStatus.DRAFT,
            TargetStatus.SCHEDULED,
            TargetStatus.FAILED,
            TargetStatus.UNCONFIRMED,
        ):
            raise ValidationError("Only pending publications can be cancelled.")
        target.status = TargetStatus.CANCELLED
        target.save(update_fields=["status", "updated_at"])
        target.post.refresh_status()
        scheduling.note_followers(
            target, f"{target.channel.name} was cancelled, so there is nothing to link to.", fail=True
        )
        return Response(TargetSerializer(target, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Settles an unconfirmed publication: {"published": true, "url": ...} or {"published": false}."""
        target = self.get_object()
        if target.status != TargetStatus.UNCONFIRMED:
            raise ValidationError("Only unconfirmed publications need checking.")
        published = request.data.get("published")
        if published not in (True, False):
            raise ValidationError({"published": "Say whether the post is live (true) or not (false)."})
        extra = {k: v for k, v in (target.extra or {}).items() if k != "maybe_live_since"}
        if published:
            url = (request.data.get("url") or "").strip()
            if url and not URL_RE.match(url):
                raise ValidationError({"url": "Enter the post's address, starting with https://."})
            waiting = target.followers.filter(status=TargetStatus.SCHEDULED, scheduled_at__isnull=True)
            if not url and waiting.exists():
                raise ValidationError({"url": "Publications waiting for this one need its address."})
            now = timezone.now()
            target.status = TargetStatus.PUBLISHED
            target.published_at = now
            target.external_url = url[:1000]
            target.last_error = "Marked as published by hand: statistics are not collected for it."
            target.next_metrics_at = None
        else:
            target.status = TargetStatus.SCHEDULED
            target.scheduled_at = timezone.now()
            target.attempts = 0
            target.last_error = ""
        target.next_attempt_at = None
        target.extra = extra
        target.save()
        target.post.refresh_status()
        if published:
            with transaction.atomic():
                scheduling.release_followers(target)
        else:
            from .tasks import dispatch_due_targets

            dispatch_due_targets.delay()
        return Response(TargetSerializer(target, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"], url_path="edit-remote")
    def edit_remote(self, request, pk=None):
        """Saves a new text/title for a published post and changes it on the network."""
        from .tasks import update_remote

        target = self.get_object()
        if target.status != TargetStatus.PUBLISHED or not target.external_id:
            raise ValidationError("Only published posts can be edited on the network.")
        if not target.channel.get_provider().capabilities.supports_edit:
            raise ValidationError(f"{target.channel.get_provider().label} posts cannot be edited from Markette.")
        before = (target.body, target.title)
        if "body" in request.data:
            target.body = str(request.data.get("body") or "")
        if "title" in request.data:
            target.title = str(request.data.get("title") or "")[:300]
        try:
            update_remote(target)
        except ProviderError as exc:
            target.body, target.title = before
            raise ValidationError(str(exc)) from exc
        target.save(update_fields=["body", "title", "updated_at"])
        return Response(TargetSerializer(target, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"], url_path="continue-thread")
    def continue_thread(self, request, pk=None):
        """Posts the remaining parts of a thread that stopped part-way."""
        from .tasks import continue_thread

        target = self.get_object()
        thread = (target.extra or {}).get("thread")
        if target.status != TargetStatus.PUBLISHED or not thread or len(thread["posts"]) >= thread["total"]:
            raise ValidationError("This publication has no unfinished thread.")
        try:
            continue_thread(target)
        except ProviderError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(TargetSerializer(target, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"], url_path="delete-remote")
    def delete_remote(self, request, pk=None):
        target = self.get_object()
        if target.status != TargetStatus.PUBLISHED or not target.external_id:
            raise ValidationError("Nothing to delete on the network.")
        try:
            target.channel.get_provider().delete(target.external_id)
        except ProviderError as exc:
            raise ValidationError(str(exc)) from exc
        target.status = TargetStatus.CANCELLED
        target.last_error = "Deleted from the network."
        target.next_metrics_at = None
        target.save(update_fields=["status", "last_error", "next_metrics_at", "updated_at"])
        target.post.refresh_status()
        return Response(TargetSerializer(target, context=self.get_serializer_context()).data)


class CalendarViewSet(WorkspaceScopedMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    """Everything scheduled or published in [start, end), one row per channel publication."""

    queryset = PostTarget.objects.select_related("channel", "post").prefetch_related("post__postmedia_set__asset")
    serializer_class = CalendarItemSerializer
    workspace_field = "post__workspace"
    pagination_class = None

    def get_queryset(self):
        qs = access.filter_channels(super().get_queryset(), self.membership, "channel__")
        qs = qs.exclude(status=TargetStatus.CANCELLED)
        if team := self.request.query_params.get("team"):
            qs = qs.filter(channel__teams=team)
        start = parse_datetime(self.request.query_params.get("start") or "")
        end = parse_datetime(self.request.query_params.get("end") or "")
        if not start or not end:
            raise ValidationError("start and end (ISO datetimes) are required.")
        when = Q(scheduled_at__gte=start, scheduled_at__lt=end) | Q(published_at__gte=start, published_at__lt=end)
        qs = qs.filter(when)
        if channels := self.request.query_params.get("channels"):
            qs = qs.filter(channel_id__in=[int(c) for c in channels.split(",") if c.isdigit()])
        if campaign := self.request.query_params.get("campaign"):
            qs = qs.filter(post__campaign_id=campaign)
        return qs.order_by("scheduled_at")

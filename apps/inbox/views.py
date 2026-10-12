from django.db.models import Q
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.channels.models import Channel
from apps.channels.providers import ProviderError
from apps.teams import access
from apps.tenants.tenancy import WorkspaceScopedMixin

from . import tasks
from .models import InboxItem, InboxState, ItemStatus


class InboxItemSerializer(serializers.ModelSerializer):
    channel_name = serializers.CharField(source="channel.name", read_only=True)
    provider = serializers.CharField(source="channel.provider", read_only=True)
    post = serializers.IntegerField(source="target.post_id", read_only=True, default=None)
    post_title = serializers.SerializerMethodField()
    can_reply = serializers.SerializerMethodField()
    replied_by_name = serializers.SerializerMethodField()

    class Meta:
        model = InboxItem
        fields = (
            "id",
            "channel",
            "channel_name",
            "provider",
            "kind",
            "text",
            "url",
            "author_name",
            "author_handle",
            "author_avatar",
            "author_url",
            "remote_created_at",
            "target",
            "post",
            "post_title",
            "status",
            "reply_text",
            "reply_url",
            "replied_at",
            "replied_by_name",
            "can_reply",
        )
        read_only_fields = fields

    def get_post_title(self, obj) -> str:
        t = obj.target
        if t is None:
            return ""
        return t.effective_title or (t.effective_body or "").strip().split("\n")[0][:120]

    def get_can_reply(self, obj) -> bool:
        return obj.channel.get_provider().capabilities.inbox_reply

    def get_replied_by_name(self, obj) -> str | None:
        u = obj.replied_by
        return (u.name or u.email) if u else None


class InboxViewSet(WorkspaceScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Replies, mentions and comments on the organization's channels this member can use."""

    serializer_class = InboxItemSerializer
    queryset = InboxItem.objects.select_related("channel", "target__post", "replied_by")
    filterset_fields = ("channel", "kind")

    def get_queryset(self):
        ws = self.workspace
        qs = self.queryset.filter(channel__organization_id=ws.organization_id)
        qs = access.filter_channels(qs, self.membership, "channel__")
        status = self.request.query_params.get("status") or "open"
        if status == "open":
            qs = qs.exclude(status=ItemStatus.DONE)
        elif status != "all":
            qs = qs.filter(status=status)
        if q := (self.request.query_params.get("q") or "").strip():
            qs = qs.filter(Q(text__icontains=q) | Q(author_name__icontains=q) | Q(author_handle__icontains=q))
        return qs

    def _visible(self):
        ws = self.workspace
        qs = InboxItem.objects.filter(channel__organization_id=ws.organization_id)
        return access.filter_channels(qs, self.membership, "channel__")

    @action(detail=False, methods=["post"])
    def mark(self, request):
        """{"ids": [...], "status": "read" | "done" | "new"}"""
        status = request.data.get("status")
        if status not in ItemStatus.values:
            raise ValidationError({"status": "read, done or new."})
        ids = [int(i) for i in request.data.get("ids") or [] if str(i).isdigit()]
        n = self._visible().filter(pk__in=ids).update(status=status)
        return Response({"updated": n})

    @action(detail=True, methods=["post"])
    def reply(self, request, pk=None):
        item = self.get_object()
        text = (request.data.get("text") or "").strip()
        if not text:
            raise ValidationError({"text": "Write a reply."})
        try:
            tasks.reply(item, text, request.user)
        except ProviderError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(self.get_serializer(item).data)

    @action(detail=False)
    def summary(self, request):
        """Unread count (for the menu badge) and each channel's polling state."""
        new = self._visible().filter(status=ItemStatus.NEW).count()
        channels = access.filter_channels(
            Channel.objects.filter(organization_id=self.workspace.organization_id), self.membership
        )
        rows = []
        states = {s.channel_id: s for s in InboxState.objects.filter(channel__in=channels)}
        for ch in channels:
            caps = ch.get_provider().capabilities
            if not caps.inbox:
                continue
            st = states.get(ch.pk)
            rows.append(
                {
                    "channel": ch.pk,
                    "name": ch.name,
                    "provider": ch.provider,
                    "can_reply": caps.inbox_reply,
                    "last_polled_at": st.last_polled_at if st else None,
                    "last_error": st.last_error if st else "",
                }
            )
        return Response({"new": new, "channels": rows})

    @action(detail=False, methods=["post"])
    def refresh(self, request):
        """Polls the member's channels now (in the background)."""
        channels = access.filter_channels(
            Channel.objects.filter(organization_id=self.workspace.organization_id, status="active"), self.membership
        )
        n = 0
        for ch in channels:
            if ch.get_provider().capabilities.inbox:
                InboxState.objects.filter(channel=ch).update(next_poll_at=None)
                tasks.poll_channel.delay(ch.pk)
                n += 1
        return Response({"polling": n})

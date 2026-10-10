from django.db import transaction
from rest_framework import serializers

from apps.channels.models import Channel
from apps.channels.serializers import ChannelSerializer
from apps.teams import access

from .models import Campaign, MediaAsset, Post, PostingSlot, PostMedia, PostTarget, ScheduleMode, TargetStatus


class MediaAssetSerializer(serializers.ModelSerializer):
    url = serializers.CharField(source="public_url", read_only=True)

    class Meta:
        model = MediaAsset
        fields = ("id", "url", "original_name", "mime", "size", "width", "height", "alt_text", "created_at")
        read_only_fields = ("id", "url", "original_name", "mime", "size", "width", "height", "created_at")


class CampaignSerializer(serializers.ModelSerializer):
    post_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Campaign
        fields = ("id", "name", "brief", "starts_on", "ends_on", "color", "post_count", "created_at")
        read_only_fields = ("id", "created_at")

    def validate(self, attrs):
        start = attrs.get("starts_on", getattr(self.instance, "starts_on", None))
        end = attrs.get("ends_on", getattr(self.instance, "ends_on", None))
        if start and end and end < start:
            raise serializers.ValidationError({"ends_on": "The campaign ends before it starts."})
        return attrs


class SlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostingSlot
        fields = ("id", "channel", "team", "weekday", "time", "active")

    def validate(self, attrs):
        if attrs.get("channel") and attrs.get("team"):
            raise serializers.ValidationError("A slot belongs to a channel or a team, not both.")
        return attrs

    def validate_team(self, team):
        if team and team.workspace_id != self.context["workspace"].id:
            raise serializers.ValidationError("Unknown team.")
        return team

    def validate_weekday(self, v):
        if not 0 <= v <= 6:
            raise serializers.ValidationError("0 (Monday) to 6 (Sunday).")
        return v

    def validate_channel(self, channel):
        if channel and channel.organization_id != self.context["workspace"].organization_id:
            raise serializers.ValidationError("Unknown channel.")
        return channel


class TargetSerializer(serializers.ModelSerializer):
    channel = serializers.PrimaryKeyRelatedField(queryset=Channel.objects.all())
    channel_detail = ChannelSerializer(source="channel", read_only=True)

    class Meta:
        model = PostTarget
        fields = (
            "id",
            "channel",
            "channel_detail",
            "body",
            "title",
            "media_ids",
            "options",
            "status",
            "scheduled_at",
            "schedule_reason",
            "attempts",
            "next_attempt_at",
            "published_at",
            "external_id",
            "external_url",
            "last_error",
            "extra",
            "metrics",
            "metrics_updated_at",
        )
        read_only_fields = (
            "extra",
            "id",
            "status",
            "schedule_reason",
            "attempts",
            "next_attempt_at",
            "published_at",
            "external_id",
            "external_url",
            "last_error",
            "metrics",
            "metrics_updated_at",
        )
        extra_kwargs = {"scheduled_at": {"read_only": True}}

    def validate_channel(self, channel):
        if channel.organization_id != self.context["workspace"].organization_id:
            raise serializers.ValidationError("Unknown channel.")
        membership = self.context.get("membership")
        if membership and not access.can_use_channel(membership, channel):
            raise serializers.ValidationError("You are not in a team that publishes to this channel.")
        return channel


class PostSerializer(serializers.ModelSerializer):
    targets = TargetSerializer(many=True, required=False)
    media_ids = serializers.ListField(child=serializers.IntegerField(), write_only=True, required=False)
    media = MediaAssetSerializer(source="ordered_media", many=True, read_only=True)
    author_name = serializers.SerializerMethodField()
    submitted_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    needs_approval = serializers.SerializerMethodField()
    can_review = serializers.SerializerMethodField()
    hidden_targets = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = (
            "id",
            "title",
            "body",
            "link",
            "tags",
            "campaign",
            "notes",
            "status",
            "schedule_mode",
            "window_start",
            "window_end",
            "media",
            "media_ids",
            "targets",
            "author_name",
            "approval_status",
            "submitted_by_name",
            "submitted_at",
            "reviewed_by_name",
            "reviewed_at",
            "needs_approval",
            "can_review",
            "hidden_targets",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "status",
            "schedule_mode",
            "window_start",
            "window_end",
            "approval_status",
            "submitted_at",
            "reviewed_at",
            "created_at",
            "updated_at",
        )

    def get_author_name(self, obj) -> str:
        return (obj.author.name or obj.author.email) if obj.author else ""

    def get_submitted_by_name(self, obj) -> str | None:
        return (obj.submitted_by.name or obj.submitted_by.email) if obj.submitted_by else None

    def get_reviewed_by_name(self, obj) -> str | None:
        return (obj.reviewed_by.name or obj.reviewed_by.email) if obj.reviewed_by else None

    def _membership(self):
        return self.context.get("membership")

    def get_needs_approval(self, obj) -> bool:
        m = self._membership()
        return bool(m and access.needs_approval(obj, m))

    def get_can_review(self, obj) -> bool:
        m = self._membership()
        return bool(m and obj.approval_status == "pending" and access.can_review(obj, m))

    def get_hidden_targets(self, obj) -> int:
        """Publications on channels this user cannot see (kept untouched on save)."""
        m = self._membership()
        if not m or access.is_unrestricted(m):
            return 0
        return sum(1 for t in obj.targets.all() if not access.can_use_channel(m, t.channel))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        m = self._membership()
        if m and not access.is_unrestricted(m):
            visible = {t.pk for t in instance.targets.all() if access.can_use_channel(m, t.channel)}
            data["targets"] = [t for t in data["targets"] if t["id"] in visible]
        return data

    def validate_campaign(self, campaign):
        if campaign and campaign.workspace_id != self.context["workspace"].id:
            raise serializers.ValidationError("Unknown campaign.")
        return campaign

    def validate_media_ids(self, ids):
        ws = self.context["workspace"]
        found = set(MediaAsset.objects.filter(workspace=ws, id__in=ids).values_list("id", flat=True))
        if missing := [i for i in ids if i not in found]:
            raise serializers.ValidationError(f"Unknown media: {missing}")
        return ids

    def _set_media(self, post, ids):
        PostMedia.objects.filter(post=post).delete()
        PostMedia.objects.bulk_create([PostMedia(post=post, asset_id=i, position=n) for n, i in enumerate(ids)])

    def _sync_targets(self, post, targets):
        """Upsert targets by channel; targets not listed are removed unless already live."""
        keep = []
        for data in targets:
            channel = data.pop("channel")
            t = PostTarget.objects.filter(post=post, channel=channel).first()
            if t and t.status in (TargetStatus.PUBLISHED, TargetStatus.PUBLISHING, TargetStatus.QUEUED):
                keep.append(t.pk)
                continue
            if t is None:
                t = PostTarget(post=post, channel=channel)
            for k, v in data.items():
                setattr(t, k, v)
            t.save()
            keep.append(t.pk)
        stale = post.targets.exclude(pk__in=keep).exclude(
            status__in=(TargetStatus.PUBLISHED, TargetStatus.PUBLISHING, TargetStatus.QUEUED)
        )
        m = self._membership()
        if m and not access.is_unrestricted(m):
            # Never drop publications on channels the editor cannot even see.
            stale = [t for t in stale.select_related("channel") if access.can_use_channel(m, t.channel)]
            PostTarget.objects.filter(pk__in=[t.pk for t in stale]).delete()
        else:
            stale.delete()

    @transaction.atomic
    def create(self, validated):
        targets = validated.pop("targets", [])
        media_ids = validated.pop("media_ids", [])
        post = Post.objects.create(**validated)
        self._set_media(post, media_ids)
        self._sync_targets(post, targets)
        return post

    @transaction.atomic
    def update(self, post, validated):
        targets = validated.pop("targets", None)
        media_ids = validated.pop("media_ids", None)
        for k, v in validated.items():
            setattr(post, k, v)
        post.save()
        if media_ids is not None:
            self._set_media(post, media_ids)
        if targets is not None:
            self._sync_targets(post, targets)
        post.refresh_status()
        return post


class ScheduleSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=ScheduleMode.choices)
    at = serializers.DateTimeField(required=False, allow_null=True)
    window_start = serializers.DateTimeField(required=False, allow_null=True)
    window_end = serializers.DateTimeField(required=False, allow_null=True)
    # {"<target id>": iso datetime} or {"channel:<id>": iso datetime}
    per_target = serializers.DictField(child=serializers.DateTimeField(), required=False)

    def validate(self, attrs):
        ws, we = attrs.get("window_start"), attrs.get("window_end")
        if ws and we and we <= ws:
            raise serializers.ValidationError({"window_end": "The window must end after it starts."})
        per = {}
        for k, v in (attrs.get("per_target") or {}).items():
            per[int(k) if str(k).isdigit() else k] = v
        attrs["per_target"] = per
        return attrs


class CalendarItemSerializer(serializers.ModelSerializer):
    post_id = serializers.IntegerField(source="post.id")
    post_title = serializers.CharField(source="post.title")
    excerpt = serializers.SerializerMethodField()
    channel_name = serializers.CharField(source="channel.name")
    provider = serializers.CharField(source="channel.provider")
    avatar_url = serializers.CharField(source="channel.avatar_url")
    campaign = serializers.IntegerField(source="post.campaign_id", allow_null=True)
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = PostTarget
        fields = (
            "id",
            "post_id",
            "post_title",
            "excerpt",
            "channel",
            "channel_name",
            "provider",
            "avatar_url",
            "status",
            "scheduled_at",
            "published_at",
            "external_url",
            "schedule_reason",
            "campaign",
            "thumbnail",
            "metrics",
        )

    def get_excerpt(self, obj) -> str:
        text = obj.effective_body or obj.effective_title
        return text[:140]

    def get_thumbnail(self, obj) -> str | None:
        media = obj.post.ordered_media()
        img = next((m for m in media if m.mime.startswith("image/")), None)
        return img.public_url if img else None

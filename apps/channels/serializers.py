from rest_framework import serializers

from .models import Channel


class ChannelSerializer(serializers.ModelSerializer):
    provider_label = serializers.CharField(source="get_provider_display", read_only=True)
    teams = serializers.SerializerMethodField()
    workspaces = serializers.SerializerMethodField()
    shared_count = serializers.SerializerMethodField()
    relevant = serializers.SerializerMethodField()
    setup = serializers.SerializerMethodField()
    approval_required = serializers.SerializerMethodField()

    def _ws_teams(self, obj):
        ws = self.context.get("workspace")
        return [t for t in obj.teams.all() if ws is None or t.workspace_id == ws.id]

    def get_teams(self, obj) -> list[dict]:
        return [{"id": t.id, "name": t.name, "color": t.color} for t in self._ws_teams(obj)]

    def _visible_workspace_ids(self):
        """Workspaces the requesting user can open; others are counted, not named."""
        if "_visible_ws" not in self.context:
            request = self.context.get("request")
            ids = None
            if request and request.user.is_authenticated and not request.user.is_superuser:
                from apps.tenants.tenancy import accessible_workspaces

                ids = set(accessible_workspaces(request.user).values_list("id", flat=True))
            self.context["_visible_ws"] = ids
        return self.context["_visible_ws"]

    def get_workspaces(self, obj) -> list[dict]:
        visible = self._visible_workspace_ids()
        return [
            {"id": w.id, "name": w.name, "slug": w.slug}
            for w in obj.workspaces.all()
            if visible is None or w.id in visible
        ]

    def get_setup(self, obj) -> dict | None:
        """Webhook details the user must configure on the platform side."""
        if obj.provider == "whatsapp":
            from .providers.whatsapp import webhook_url

            return {
                "webhook_url": webhook_url(),
                "verify_token": obj.config.get("verify_token", ""),
                "subscribers": obj.subscribers.filter(opted_out_at__isnull=True).count(),
            }
        if obj.provider == "webhook":
            m = self.context.get("membership")
            secret = obj.credentials.get("secret", "") if m is None or m.has_role("admin") else None
            return {
                "signing_secret": secret,
                "signature_header": "X-Markette-Signature",
                "timestamp_header": "X-Markette-Timestamp",
                "url": obj.credentials.get("url", ""),
            }
        if obj.provider == "qq":
            from .providers.qq import callback_url

            return {"webhook_url": callback_url(obj.credentials.get("app_id", ""))}
        return None

    def get_relevant(self, obj) -> bool:
        """Relevant for the workspace of the request (shown first and preselected there)."""
        if hasattr(obj, "is_relevant"):
            return bool(obj.is_relevant)
        ws = self.context.get("workspace")
        return bool(ws and any(w.id == ws.id for w in obj.workspaces.all()))

    def get_shared_count(self, obj) -> int:
        return len(obj.workspaces.all())

    def get_approval_required(self, obj) -> bool:
        """Whether the current user's posts to this channel go through review."""
        from apps.teams.models import TeamMember, TeamRole

        m = self.context.get("membership")
        if not m or m.has_role("admin"):
            return False
        teams = [t for t in self._ws_teams(obj) if t.requires_approval]
        if not teams:
            return False
        reviewer_of = set(
            TeamMember.objects.filter(team__in=teams, user=m.user, role=TeamRole.REVIEWER).values_list(
                "team_id", flat=True
            )
        )
        return any(t.id not in reviewer_of for t in teams)

    class Meta:
        model = Channel
        fields = (
            "id",
            "provider",
            "provider_label",
            "external_id",
            "name",
            "handle",
            "avatar_url",
            "profile_url",
            "base_url",
            "config",
            "status",
            "last_error",
            "followers_count",
            "color",
            "token_expires_at",
            "teams",
            "organization",
            "workspaces",
            "shared_count",
            "relevant",
            "setup",
            "approval_required",
            "created_at",
        )
        read_only_fields = (
            "id",
            "organization",
            "provider",
            "external_id",
            "handle",
            "avatar_url",
            "profile_url",
            "base_url",
            "status",
            "last_error",
            "followers_count",
            "token_expires_at",
            "created_at",
        )

    def validate_config(self, value):
        # Only non-structural keys are editable after connection.
        current = dict(self.instance.config) if self.instance else {}
        editable = {
            "links_in_first_comment",
            "default_status",
            "permalink",
            "site_url",
            "front_matter",
            "author_name",
            "author_email",
            "body_format",
            "tags_field",
            "image_field",
        }
        current.update({k: v for k, v in (value or {}).items() if k in editable})
        return current


class ConnectSerializer(serializers.Serializer):
    provider = serializers.CharField()
    base_url = serializers.CharField(required=False, allow_blank=True)
    credentials = serializers.DictField(required=False)


class SelectCandidatesSerializer(serializers.Serializer):
    external_ids = serializers.ListField(child=serializers.CharField(), allow_empty=False)

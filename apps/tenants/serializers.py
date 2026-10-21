import re
import zoneinfo

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.platform import conf

from . import limits
from .models import Invitation, Membership, Organization, OrgMembership, Workspace

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    is_superadmin = serializers.BooleanField(source="is_superuser", read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "name", "timezone", "is_superadmin")
        read_only_fields = ("id", "email")

    def validate_timezone(self, value):
        if value not in zoneinfo.available_timezones():
            raise serializers.ValidationError("Unknown time zone.")
        return value


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    name = serializers.CharField(required=False, allow_blank=True)
    organization_name = serializers.CharField(required=False, allow_blank=True)
    workspace_name = serializers.CharField(required=False, allow_blank=True)
    timezone = serializers.CharField(required=False, default="UTC")
    invitation = serializers.CharField(required=False, allow_blank=True)

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value.lower()

    def validate_password(self, value):
        validate_password(value)
        return value


class OrganizationSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()
    workspace_count = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "status",
            "role",
            "max_workspaces",
            "max_channels",
            "max_members",
            "ai_monthly_tokens",
            "workspace_count",
            "usage",
            "created_at",
        )
        read_only_fields = (
            "id",
            "slug",
            "status",
            "max_workspaces",
            "max_channels",
            "max_members",
            "ai_monthly_tokens",
            "created_at",
        )

    def _org_membership(self, obj):
        request = self.context.get("request")
        if not request:
            return None
        cache = self.context.setdefault("_org_roles", {})
        if obj.pk not in cache:
            cache[obj.pk] = OrgMembership.objects.filter(organization=obj, user=request.user).first()
        return cache[obj.pk]

    def get_role(self, obj) -> str | None:
        m = self._org_membership(obj)
        return m.role if m else None

    def get_usage(self, obj) -> dict | None:
        m = self._org_membership(obj)
        return limits.usage(obj) if m and m.is_admin else None

    def get_workspace_count(self, obj) -> int:
        return obj.workspaces.filter(archived_at__isnull=True).count()


class OrgCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=160)
    workspace_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    timezone = serializers.CharField(required=False, default="UTC")

    def validate_timezone(self, value):
        if value not in zoneinfo.available_timezones():
            raise serializers.ValidationError("Unknown time zone.")
        return value


ADULT_CONFIRMATION = (
    "Confirm that every person depicted is 18 or older and agreed to the publication, that the accounts used "
    "belong to the performers who authorized you to post for them, and that the records each platform "
    "requires (age and consent verification) are kept."
)


class WorkspaceSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    organization = serializers.PrimaryKeyRelatedField(queryset=Organization.objects.all(), required=False)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    organization_slug = serializers.CharField(source="organization.slug", read_only=True)
    organization_role = serializers.SerializerMethodField()
    archived = serializers.BooleanField(required=False, write_only=True)
    adult_confirmation = serializers.BooleanField(required=False, write_only=True)
    is_archived = serializers.SerializerMethodField()
    ai_available = serializers.SerializerMethodField()

    class Meta:
        model = Workspace
        fields = (
            "id",
            "name",
            "slug",
            "timezone",
            "min_gap_minutes",
            "ai_enabled",
            "ai_available",
            "link_tracking",
            "adult_content",
            "adult_confirmation",
            "adult_confirmed_at",
            "role",
            "organization",
            "organization_name",
            "organization_slug",
            "organization_role",
            "archived",
            "is_archived",
            "archived_at",
            "created_at",
        )
        read_only_fields = ("id", "slug", "archived_at", "adult_confirmed_at", "created_at")

    def get_role(self, obj) -> str | None:
        from .tenancy import effective_membership

        request = self.context.get("request")
        if not request:
            return None
        m = effective_membership(request.user, obj)
        return m.role if m else None

    def get_organization_role(self, obj) -> str | None:
        request = self.context.get("request")
        if not request:
            return None
        m = OrgMembership.objects.filter(organization_id=obj.organization_id, user=request.user).first()
        return m.role if m else None

    def validate(self, attrs):
        confirmed = attrs.pop("adult_confirmation", False)
        turning_on = attrs.get("adult_content") and not (self.instance and self.instance.adult_content)
        if turning_on and confirmed is not True:
            raise serializers.ValidationError({"adult_confirmation": ADULT_CONFIRMATION})
        return attrs

    def validate_link_tracking(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Must be an object.")
        out = {}
        for key in ("utm", "short_links"):
            if key in value:
                if not isinstance(value[key], bool):
                    raise serializers.ValidationError({key: "Must be true or false."})
                out[key] = value[key]
        medium = str(value.get("utm_medium") or "social").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", medium):
            raise serializers.ValidationError({"utm_medium": "Letters, digits, dots, dashes or underscores."})
        out["utm_medium"] = medium
        domains = []
        for d in value.get("domains") or []:
            d = str(d).strip().lower()
            d = re.sub(r"^https?://", "", d).split("/")[0].removeprefix("www.")
            if d and not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d):
                raise serializers.ValidationError({"domains": f"Not a domain: {d}"})
            if d and d not in domains:
                domains.append(d)
        out["domains"] = domains[:30]
        return out

    def get_is_archived(self, obj) -> bool:
        return obj.archived_at is not None

    def get_ai_available(self, obj) -> bool:
        """Server switch and, when the org has an AI allowance, whether any is left this month."""
        return bool(conf.get("AI_ENABLED")) and limits.ai_quota_left(obj.organization) != 0


class MembershipSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    email = serializers.EmailField(write_only=True, required=False)

    class Meta:
        model = Membership
        fields = ("id", "user", "email", "role", "created_at")
        read_only_fields = ("id", "user", "created_at")


class OrgMembershipSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    workspaces = serializers.SerializerMethodField()

    class Meta:
        model = OrgMembership
        fields = ("id", "user", "role", "workspaces", "created_at")
        read_only_fields = ("id", "user", "created_at")

    def get_workspaces(self, obj) -> list[dict]:
        rows = Membership.objects.filter(user=obj.user, workspace__organization=obj.organization).select_related(
            "workspace"
        )
        return [
            {"id": m.workspace_id, "name": m.workspace.name, "slug": m.workspace.slug, "role": m.role} for m in rows
        ]


class InvitationSerializer(serializers.ModelSerializer):
    workspace_name = serializers.CharField(source="workspace.name", read_only=True, default=None)

    class Meta:
        model = Invitation
        fields = (
            "id",
            "email",
            "role",
            "org_role",
            "workspace",
            "workspace_name",
            "token",
            "accepted_at",
            "created_at",
        )
        read_only_fields = ("id", "workspace", "token", "accepted_at", "created_at")

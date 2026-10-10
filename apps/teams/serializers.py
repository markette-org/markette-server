from rest_framework import serializers

from apps.channels.models import Channel
from apps.core.models import User
from apps.tenants.models import Membership, OrgMembership

from .models import Team, TeamMember


class TeamMemberSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    name = serializers.CharField(source="user.name", read_only=True)

    class Meta:
        model = TeamMember
        fields = ("id", "user_id", "email", "name", "role")


class TeamSerializer(serializers.ModelSerializer):
    channels = serializers.PrimaryKeyRelatedField(queryset=Channel.objects.all(), many=True, required=False)
    members = TeamMemberSerializer(many=True, read_only=True)
    my_role = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = (
            "id",
            "name",
            "description",
            "color",
            "requires_approval",
            "channels",
            "members",
            "my_role",
            "created_at",
        )
        read_only_fields = ("id", "created_at")

    def validate_channels(self, channels):
        ws = self.context["workspace"]
        if any(c.organization_id != ws.organization_id for c in channels):
            raise serializers.ValidationError("Unknown channel.")
        return channels

    def get_my_role(self, obj) -> str | None:
        request = self.context.get("request")
        m = next((m for m in obj.members.all() if request and m.user_id == request.user.id), None)
        return m.role if m else None


class AddTeamMemberSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=TeamMember._meta.get_field("role").choices, default="member")

    def validate_email(self, email):
        ws = self.context["workspace"]
        user = User.objects.filter(email__iexact=email).first()
        has_access = user and (
            Membership.objects.filter(workspace=ws, user=user).exists()
            or OrgMembership.objects.filter(
                organization=ws.organization, user=user, role__in=("owner", "admin")
            ).exists()
        )
        if not has_access:
            raise serializers.ValidationError("Add this person to the workspace first.")
        return user

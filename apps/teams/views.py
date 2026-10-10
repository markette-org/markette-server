from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.tenants.models import Role
from apps.tenants.tenancy import WorkspaceScopedMixin

from .models import Team, TeamMember
from .serializers import AddTeamMemberSerializer, TeamMemberSerializer, TeamSerializer


class TeamViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    """Everyone in the workspace can list teams; only admins change them."""

    queryset = Team.objects.prefetch_related("channels", "members__user")
    serializer_class = TeamSerializer
    pagination_class = None

    def get_permissions(self):
        if self.action not in ("list", "retrieve"):
            self.required_role = Role.ADMIN
        return super().get_permissions()

    @action(detail=True, methods=["post"], url_path="members")
    def add_member(self, request, pk=None):
        team = self.get_object()
        s = AddTeamMemberSerializer(data=request.data, context=self.get_serializer_context())
        s.is_valid(raise_exception=True)
        member, _ = TeamMember.objects.update_or_create(
            team=team, user=s.validated_data["email"], defaults={"role": s.validated_data["role"]}
        )
        return Response(TeamMemberSerializer(member).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch", "delete"], url_path=r"members/(?P<member_id>\d+)")
    def member(self, request, pk=None, member_id=None):
        team = self.get_object()
        member = get_object_or_404(TeamMember, team=team, pk=member_id)
        if request.method == "DELETE":
            member.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        s = TeamMemberSerializer(member, data={"role": request.data.get("role")}, partial=True)
        s.is_valid(raise_exception=True)
        s.save()
        return Response(s.data)

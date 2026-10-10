from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.docs import doc
from apps.platform.models import PlatformSettings
from apps.teams.models import TeamMember

from . import limits
from .models import Invitation, Membership, Organization, OrgMembership, OrgRole, Role, Workspace
from .serializers import (
    InvitationSerializer,
    MembershipSerializer,
    OrganizationSerializer,
    OrgCreateSerializer,
    OrgMembershipSerializer,
    RegisterSerializer,
    UserSerializer,
    WorkspaceSerializer,
)
from .tenancy import WorkspaceScopedMixin, accessible_workspaces, effective_membership

User = get_user_model()


def accept_invitation_for(user, token):
    inv = (
        Invitation.objects.select_related("workspace__organization", "organization")
        .filter(token=token, accepted_at__isnull=True)
        .first()
    )
    if inv is None or inv.email.lower() != user.email.lower():
        raise ValidationError({"invitation": "Invalid or already used invitation."})
    org = inv.organization or inv.workspace.organization
    limits.check_member_limit(org, user=user)
    org_role = inv.org_role or OrgRole.MEMBER
    org_m, created = OrgMembership.objects.get_or_create(organization=org, user=user, defaults={"role": org_role})
    if not created and inv.org_role and _org_rank(inv.org_role) > _org_rank(org_m.role):
        org_m.role = inv.org_role
        org_m.save(update_fields=["role"])
    if inv.workspace:
        Membership.objects.update_or_create(workspace=inv.workspace, user=user, defaults={"role": inv.role})
    inv.accepted_at = timezone.now()
    inv.save(update_fields=["accepted_at"])
    return inv.workspace or org.workspaces.filter(archived_at__isnull=True).first()


def check_signup_allowed(email):
    ps = PlatformSettings.load()
    if not ps.signups_open:
        raise PermissionDenied("Sign-up is by invitation only on this installation.")
    domains = [d.strip().lower().lstrip("@") for d in ps.signup_domains.split(",") if d.strip()]
    if domains and email.rsplit("@", 1)[-1].lower() not in domains:
        raise ValidationError({"email": "Sign-up is limited to: " + ", ".join(domains)})


def _org_rank(role):
    return {OrgRole.MEMBER: 0, OrgRole.ADMIN: 1, OrgRole.OWNER: 2}[role]


@doc(
    "auth",
    "Create an account",
    description="Creates the user, an organization and a first workspace (or accepts an invitation) and returns JWTs.",
    request=RegisterSerializer,
)
@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    s = RegisterSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    d = s.validated_data
    if not d.get("invitation"):
        check_signup_allowed(d["email"])
    with transaction.atomic():
        user = User.objects.create_user(d["email"], d["password"], name=d.get("name", ""), timezone=d["timezone"])
        if d.get("invitation"):
            accept_invitation_for(user, d["invitation"])
        else:
            label = user.name or user.email.split("@")[0]
            org = Organization.objects.create(
                name=d.get("organization_name") or d.get("workspace_name") or label,
                **PlatformSettings.load().default_limits(),
            )
            OrgMembership.objects.create(organization=org, user=user, role=OrgRole.OWNER)
            ws = Workspace.objects.create(
                organization=org, name=d.get("workspace_name") or f"{label}'s workspace", timezone=d["timezone"]
            )
            Membership.objects.create(workspace=ws, user=user, role=Role.OWNER)
    refresh = RefreshToken.for_user(user)
    return Response(
        {"user": UserSerializer(user).data, "access": str(refresh.access_token), "refresh": str(refresh)},
        status=status.HTTP_201_CREATED,
    )


@doc("auth", "Your profile", request=UserSerializer, responses=UserSerializer)
@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def me(request):
    if request.method == "PATCH":
        s = UserSerializer(request.user, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        s.save()
    return Response(UserSerializer(request.user).data)


@doc("organizations", "Accept an invitation", description='Body: {"token": "…"}. Returns the workspace you joined.')
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def accept_invitation(request):
    with transaction.atomic():
        ws = accept_invitation_for(request.user, request.data.get("token", ""))
    return Response(WorkspaceSerializer(ws, context={"request": request}).data if ws else {})


def _org_admin_or_403(user, org, owner=False):
    m = OrgMembership.objects.filter(organization=org, user=user).first()
    ok = m and (m.role == OrgRole.OWNER if owner else m.is_admin)
    if not ok:
        raise PermissionDenied("This action requires the organization " + ("owner" if owner else "admin") + " role.")
    return m


class WorkspaceViewSet(viewsets.ModelViewSet):
    """Workspaces the user can open, across organizations. Not tenant-scoped by header."""

    queryset = Workspace.objects.none()  # for schema introspection; see get_queryset
    serializer_class = WorkspaceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = accessible_workspaces(self.request.user).select_related("organization")
        qs = qs.filter(organization__status="active")
        if org := self.request.query_params.get("organization"):
            qs = qs.filter(organization_id=org)
        return qs.order_by("organization__name", "name")

    def perform_create(self, serializer):
        org = serializer.validated_data.get("organization")
        if org is None:
            admin_of = OrgMembership.objects.filter(
                user=self.request.user, role__in=(OrgRole.OWNER, OrgRole.ADMIN)
            ).select_related("organization")
            first = admin_of.first()
            if first is None:
                raise PermissionDenied("Only organization admins can create workspaces.")
            org = first.organization
        _org_admin_or_403(self.request.user, org)
        limits.check_workspace_limit(org)
        serializer.validated_data.pop("archived", None)
        ws = serializer.save(organization=org)
        Membership.objects.create(workspace=ws, user=self.request.user, role=Role.OWNER)

    def perform_update(self, serializer):
        ws = serializer.instance
        m = effective_membership(self.request.user, ws)
        if not m or not m.has_role(Role.ADMIN):
            raise PermissionDenied("This action requires the admin role.")
        serializer.validated_data.pop("organization", None)
        archived = serializer.validated_data.pop("archived", None)
        if archived is not None and archived != (ws.archived_at is not None):
            if not m.has_role(Role.OWNER):
                _org_admin_or_403(self.request.user, ws.organization)
            if not archived:
                limits.check_workspace_limit(ws.organization)
            serializer.validated_data["archived_at"] = timezone.now() if archived else None
        serializer.save()

    def perform_destroy(self, instance):
        _org_admin_or_403(self.request.user, instance.organization, owner=True)
        instance.delete()


class OrganizationViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Organizations the user belongs to (directly or through one of its workspaces)."""

    queryset = Organization.objects.none()  # for schema introspection; see get_queryset
    serializer_class = OrganizationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        u = self.request.user
        return Organization.objects.filter(Q(memberships__user=u) | Q(workspaces__memberships__user=u)).distinct()

    def create(self, request):
        """Any signed-in user can start another organization (unless a superadmin turned it off)."""
        ps = PlatformSettings.load()
        if not (ps.org_creation_open or request.user.is_superuser):
            raise PermissionDenied("Creating organizations is disabled on this installation.")
        s = OrgCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        with transaction.atomic():
            org = Organization.objects.create(name=d["name"], **ps.default_limits())
            OrgMembership.objects.create(organization=org, user=request.user, role=OrgRole.OWNER)
            ws = Workspace.objects.create(
                organization=org, name=d.get("workspace_name") or d["name"], timezone=d.get("timezone") or "UTC"
            )
            Membership.objects.create(workspace=ws, user=request.user, role=Role.OWNER)
        return Response(
            {
                "organization": OrganizationSerializer(org, context={"request": request}).data,
                "workspace": WorkspaceSerializer(ws, context={"request": request}).data,
            },
            status=status.HTTP_201_CREATED,
        )

    def perform_update(self, serializer):
        _org_admin_or_403(self.request.user, serializer.instance)
        serializer.save()

    @action(detail=True, methods=["post"])
    def leave(self, request, pk=None):
        """Leave an organization: drops the org role and every workspace membership in it."""
        org = self.get_object()
        mine = OrgMembership.objects.filter(organization=org, user=request.user).first()
        if (
            mine
            and mine.role == OrgRole.OWNER
            and not OrgMembership.objects.filter(organization=org, role=OrgRole.OWNER).exclude(pk=mine.pk).exists()
        ):
            raise ValidationError("You are the last owner. Make someone else an owner first.")
        with transaction.atomic():
            Membership.objects.filter(user=request.user, workspace__organization=org).delete()
            TeamMember.objects.filter(user=request.user, team__workspace__organization=org).delete()
            if mine:
                mine.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"])
    def channels(self, request, pk=None):
        """Every channel of the organization with the workspaces it is shared with."""
        from apps.channels.models import Channel
        from apps.channels.serializers import ChannelSerializer

        org = self.get_object()
        _org_admin_or_403(request.user, org)
        qs = Channel.objects.filter(organization=org).prefetch_related("workspaces", "teams")
        return Response(ChannelSerializer(qs, many=True, context={"request": request}).data)

    @action(detail=True, methods=["patch", "delete"], url_path=r"channels/(?P<channel_id>\d+)")
    def channel(self, request, pk=None, channel_id=None):
        """Set the workspaces the channel is relevant for (PATCH {workspaces: [ids]}) or disconnect it (DELETE)."""
        from apps.channels.models import Channel
        from apps.channels.serializers import ChannelSerializer
        from apps.channels.services import set_channel_workspaces

        org = self.get_object()
        _org_admin_or_403(request.user, org)
        channel = get_object_or_404(Channel, organization=org, pk=channel_id)
        if request.method == "DELETE":
            channel.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        ids = request.data.get("workspaces")
        if not isinstance(ids, list):
            raise ValidationError({"workspaces": "A list of workspace ids (may be empty)."})
        workspaces = list(org.workspaces.filter(id__in=ids))
        if len(workspaces) != len(set(ids)):
            raise ValidationError({"workspaces": "Unknown workspace."})
        set_channel_workspaces(channel, workspaces)
        channel.refresh_from_db()
        return Response(ChannelSerializer(channel, context={"request": request}).data)

    @action(detail=True, methods=["get"])
    def members(self, request, pk=None):
        org = self.get_object()
        _org_admin_or_403(request.user, org)
        rows = OrgMembership.objects.filter(organization=org).select_related("user", "organization")
        return Response(OrgMembershipSerializer(rows, many=True).data)

    @action(detail=True, methods=["patch", "delete"], url_path=r"members/(?P<member_id>\d+)")
    def member(self, request, pk=None, member_id=None):
        org = self.get_object()
        me = _org_admin_or_403(request.user, org)
        target = get_object_or_404(OrgMembership, organization=org, pk=member_id)
        owners = OrgMembership.objects.filter(organization=org, role=OrgRole.OWNER)
        if request.method == "DELETE":
            if target.role == OrgRole.OWNER and owners.count() == 1:
                raise ValidationError("An organization needs at least one owner.")
            if target.role == OrgRole.OWNER and me.role != OrgRole.OWNER:
                raise PermissionDenied("Only owners can remove an owner.")
            with transaction.atomic():
                Membership.objects.filter(user=target.user, workspace__organization=org).delete()
                target.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        role = request.data.get("role")
        if role not in OrgRole.values:
            raise ValidationError({"role": "owner, admin or member."})
        if OrgRole.OWNER in (role, target.role) and me.role != OrgRole.OWNER:
            raise PermissionDenied("Only owners can grant or remove the owner role.")
        if target.role == OrgRole.OWNER and role != OrgRole.OWNER and owners.count() == 1:
            raise ValidationError("An organization needs at least one owner.")
        target.role = role
        target.save(update_fields=["role", "updated_at"])
        return Response(OrgMembershipSerializer(target).data)

    @action(detail=True, methods=["get", "post"])
    def invitations(self, request, pk=None):
        org = self.get_object()
        _org_admin_or_403(request.user, org)
        if request.method == "GET":
            rows = Invitation.objects.filter(organization=org, accepted_at__isnull=True)
            return Response(InvitationSerializer(rows, many=True).data)
        email = (request.data.get("email") or "").strip().lower()
        org_role = request.data.get("org_role") or OrgRole.MEMBER
        if not email:
            raise ValidationError({"email": "Required."})
        if org_role not in OrgRole.values:
            raise ValidationError({"org_role": "owner, admin or member."})
        limits.check_member_limit(org, email=email)
        inv = Invitation.objects.create(organization=org, email=email, org_role=org_role, invited_by=request.user)
        return Response(InvitationSerializer(inv).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["delete"], url_path=r"invitations/(?P<invitation_id>\d+)")
    def invitation(self, request, pk=None, invitation_id=None):
        org = self.get_object()
        _org_admin_or_403(request.user, org)
        get_object_or_404(Invitation, organization=org, pk=invitation_id, accepted_at__isnull=True).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MemberViewSet(
    WorkspaceScopedMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Direct workspace members. Org owners/admins have access without being listed here."""

    queryset = Membership.objects.select_related("user")
    serializer_class = MembershipSerializer
    allowed_when_archived = True

    def get_permissions(self):
        self.required_role = None if self.action == "list" else Role.ADMIN
        return super().get_permissions()

    def perform_create(self, serializer):
        """Add someone who already belongs to the organization (others need an invitation)."""
        email = (serializer.validated_data.pop("email", "") or "").lower()
        org = self.workspace.organization
        user = User.objects.filter(email__iexact=email).first() if email else None
        if user is None or not OrgMembership.objects.filter(organization=org, user=user).exists():
            raise ValidationError({"email": "Not a member of this organization yet; send an invitation instead."})
        role = serializer.validated_data.get("role", Role.EDITOR)
        if role == Role.OWNER and self.membership.role != Role.OWNER:
            raise PermissionDenied("Only owners can add owners.")
        if Membership.objects.filter(workspace=self.workspace, user=user).exists():
            raise ValidationError({"email": "Already a member of this workspace."})
        serializer.save(workspace=self.workspace, user=user)

    def perform_update(self, serializer):
        role = serializer.validated_data.get("role")
        if Role.OWNER in (role, serializer.instance.role) and self.membership.role != Role.OWNER:
            raise PermissionDenied("Only owners can grant or remove the owner role.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.role == Role.OWNER and self.membership.role != Role.OWNER:
            raise PermissionDenied("Only owners can remove an owner.")
        instance.delete()


class InvitationViewSet(
    WorkspaceScopedMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Invitation.objects.filter(accepted_at__isnull=True)
    serializer_class = InvitationSerializer
    required_role = Role.ADMIN

    def perform_create(self, serializer):
        limits.check_member_limit(self.workspace.organization, email=serializer.validated_data["email"])
        serializer.save(
            workspace=self.workspace, organization=self.workspace.organization, invited_by=self.request.user
        )

"""Request-scoped tenant resolution.

The client sends the active workspace in the ``X-Workspace`` header (slug or id).
A user's role in a workspace is the higher of:

* their workspace membership role, and
* their organization role: org owners act as workspace owners, org admins as
  workspace admins, in every workspace of the organization.

Views built on ``WorkspaceScopedMixin`` only ever see rows of that workspace.
Suspended organizations and archived workspaces are read-only and refuse
writes; superadmins use the platform API, not tenant impersonation.
"""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import permissions
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from .models import ROLE_RANK, Membership, OrgMembership, OrgRole, Role, Workspace

SAFE_ROLE = Role.VIEWER
WRITE_ROLE = Role.EDITOR
ORG_TO_WORKSPACE_ROLE = {OrgRole.OWNER: Role.OWNER, OrgRole.ADMIN: Role.ADMIN}


def effective_membership(user, workspace):
    """The Membership that governs ``user`` in ``workspace`` (possibly unsaved), or None."""
    m = Membership.objects.filter(workspace=workspace, user=user).first()
    org_m = OrgMembership.objects.filter(organization_id=workspace.organization_id, user=user).first()
    org_role = ORG_TO_WORKSPACE_ROLE.get(org_m.role) if org_m else None
    if org_role and (m is None or ROLE_RANK[org_role] > ROLE_RANK[m.role]):
        m = Membership(workspace=workspace, user=user, role=org_role)
    if m is not None:
        m.workspace = workspace
        m.org_membership = org_m
    return m


def accessible_workspaces(user):
    """Workspaces a user can open: direct memberships plus every workspace of orgs they administer."""
    admin_orgs = OrgMembership.objects.filter(user=user, role__in=(OrgRole.OWNER, OrgRole.ADMIN)).values(
        "organization_id"
    )

    return Workspace.objects.filter(Q(memberships__user=user) | Q(organization_id__in=admin_orgs)).distinct()


def resolve_membership(request):
    if hasattr(request, "_membership"):
        return request._membership
    ref = request.headers.get("X-Workspace") or request.query_params.get("workspace")
    if not ref:
        raise ValidationError({"workspace": "Send the active workspace in the X-Workspace header."})
    lookup = {"pk": ref} if str(ref).isdigit() else {"slug": ref}
    workspace = Workspace.objects.select_related("organization").filter(**lookup).first()
    membership = effective_membership(request.user, workspace) if workspace else None
    if membership is None:
        raise NotFound("Workspace not found.")
    if not workspace.organization.is_active:
        raise PermissionDenied("This organization is suspended. Contact support.")
    request._membership = membership
    return membership


class WorkspaceRolePermission(permissions.BasePermission):
    """Viewers read, editors write. Views can raise the bar with ``required_role``."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        membership = resolve_membership(request)
        writing = request.method not in permissions.SAFE_METHODS
        needed = getattr(view, "required_role", None)
        if needed is None:
            needed = WRITE_ROLE if writing else SAFE_ROLE
        if not membership.has_role(needed):
            raise PermissionDenied(f"This action requires the {needed} role.")
        if writing and membership.workspace.archived_at and not getattr(view, "allowed_when_archived", False):
            raise PermissionDenied("This workspace is archived and read-only.")
        return True


class WorkspaceScopedMixin:
    permission_classes = [WorkspaceRolePermission]
    workspace_field = "workspace"

    @property
    def membership(self):
        return resolve_membership(self.request)

    @property
    def workspace(self):
        return self.membership.workspace

    def get_queryset(self):
        return super().get_queryset().filter(**{self.workspace_field: self.workspace})

    def perform_create(self, serializer):
        serializer.save(**{self.workspace_field: self.workspace})

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request and self.request.user.is_authenticated and "X-Workspace" in self.request.headers:
            ctx["workspace"] = self.workspace
            ctx["membership"] = self.membership
        return ctx


def get_scoped_or_404(model, workspace, **lookup):
    return get_object_or_404(model, workspace=workspace, **lookup)

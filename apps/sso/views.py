from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.models import update_last_login
from django.shortcuts import get_object_or_404, redirect
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.core.docs import doc, hidden, query
from apps.tenants.models import Organization, OrgMembership, OrgRole
from apps.tenants.serializers import UserSerializer

from . import service
from .models import SSOConfig, SSODomain

# --- sign-in -------------------------------------------------------------------------


@doc(
    "auth",
    "Find the SSO provider for an email",
    params=[query("email", "The address being signed in", required=True), query("next", "Path to open after sign-in")],
)
@api_view(["GET"])
@permission_classes([AllowAny])
def discover(request):
    """Does this email sign in through an organization's identity provider?"""
    email = request.query_params.get("email", "")
    cfg = service.config_for_email(email) if "@" in email else None
    if cfg is None:
        return Response({"sso": False})
    query = urlencode({"next": request.query_params.get("next", "")}) if request.query_params.get("next") else ""
    return Response(
        {
            "sso": True,
            "organization": cfg.organization.name,
            "enforced": cfg.enforce,
            "start_url": f"{settings.PUBLIC_API_URL}/api/auth/sso/start/{cfg.organization.slug}/"
            + (f"?{query}" if query else ""),
        }
    )


@hidden
@api_view(["GET"])
@permission_classes([AllowAny])
def start(request, org_slug):
    cfg = SSOConfig.objects.filter(organization__slug=org_slug, enabled=True).first()
    if cfg is None:
        return redirect(f"{settings.FRONTEND_URL}/login?{urlencode({'sso_error': 'Single sign-on is not enabled.'})}")
    try:
        return redirect(service.authorization_url(cfg, request.query_params.get("next", "")))
    except service.SSOError as exc:
        return redirect(f"{settings.FRONTEND_URL}/login?{urlencode({'sso_error': str(exc)[:300]})}")


@hidden
@api_view(["GET"])
@permission_classes([AllowAny])
def callback(request):
    try:
        ticket = service.complete(request.query_params)
    except (service.SSOError, ValidationError) as exc:
        message = str(exc.detail if isinstance(exc, ValidationError) else exc)[:300]
        return redirect(f"{settings.FRONTEND_URL}/login?{urlencode({'sso_error': message})}")
    return redirect(f"{settings.FRONTEND_URL}/sso/complete?{urlencode({'ticket': ticket.code})}")


@doc(
    "auth",
    "Exchange an SSO ticket for tokens",
    description='Body: {"ticket": "…"} (from /sso/complete?ticket=…). Single use.',
)
@api_view(["POST"])
@permission_classes([AllowAny])
def exchange(request):
    user, next_path = service.redeem(request.data.get("ticket", ""))
    refresh = RefreshToken.for_user(user)
    update_last_login(None, user)
    return Response(
        {
            "user": UserSerializer(user).data,
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "next": next_path,
        }
    )


class PasswordTokenSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        if service.password_login_blocked(self.user):
            raise PermissionDenied("Your organization requires single sign-on. Use 'Continue with SSO'.")
        return data


class PasswordTokenView(TokenObtainPairView):
    serializer_class = PasswordTokenSerializer


# --- configuration (organization owners) ----------------------------------------------


def _owner_org(request, org_id, read=False):
    org = get_object_or_404(Organization, pk=org_id)
    m = OrgMembership.objects.filter(organization=org, user=request.user).first()
    allowed = (OrgRole.OWNER, OrgRole.ADMIN) if read else (OrgRole.OWNER,)
    if not (m and m.role in allowed) and not request.user.is_superuser:
        raise PermissionDenied("Only organization owners configure single sign-on.")
    return org


def _describe(org, cfg):
    return {
        "configured": cfg is not None,
        "redirect_uri": service.redirect_uri(),
        "enabled": bool(cfg and cfg.enabled),
        "issuer": cfg.issuer if cfg else "",
        "client_id": cfg.client_id if cfg else "",
        "client_secret_set": bool(cfg and cfg.secrets.get("client_secret")),
        "scopes": cfg.scopes if cfg else "openid email profile",
        "auto_provision": cfg.auto_provision if cfg else True,
        "default_org_role": cfg.default_org_role if cfg else OrgRole.MEMBER,
        "default_workspace": cfg.default_workspace_id if cfg else None,
        "default_workspace_role": cfg.default_workspace_role if cfg else "editor",
        "enforce": bool(cfg and cfg.enforce),
        "login_url": f"{settings.PUBLIC_API_URL}/api/auth/sso/start/{org.slug}/",
        "domains": [
            {
                "id": d.id,
                "domain": d.domain,
                "verified": d.verified_at is not None,
                "verified_at": d.verified_at,
                "txt_name": d.txt_name,
                "txt_value": d.txt_value,
            }
            for d in (cfg.domains.order_by("domain") if cfg else [])
        ],
    }


@doc(
    "organizations",
    "Single sign-on settings",
    description="GET for owners and admins; PUT for owners. client_secret is write-only.",
)
@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def config(request, org_id):
    org = _owner_org(request, org_id, read=request.method == "GET")
    cfg = SSOConfig.objects.filter(organization=org).first()
    if request.method == "GET":
        data = _describe(org, cfg)
        data["can_edit"] = (
            request.user.is_superuser
            or OrgMembership.objects.filter(organization=org, user=request.user, role=OrgRole.OWNER).exists()
        )
        return Response(data)
    d = request.data
    if cfg and d.get("enabled") is False and not d.get("issuer"):
        # Turning SSO off needs nothing else.
        cfg.enabled = cfg.enforce = False
        cfg.save(update_fields=["enabled", "enforce", "updated_at"])
        return Response(_describe(org, cfg))
    issuer = (d.get("issuer") or "").strip().rstrip("/")
    client_id = (d.get("client_id") or "").strip()
    if not issuer.startswith("https://") and not settings.DEBUG:
        raise ValidationError({"issuer": "The issuer must be an https:// URL."})
    if not issuer or not client_id:
        raise ValidationError({"issuer": "Issuer and client ID are required."})
    role = d.get("default_org_role", OrgRole.MEMBER)
    if role not in (OrgRole.MEMBER, OrgRole.ADMIN):
        raise ValidationError({"default_org_role": "member or admin."})
    ws_id = d.get("default_workspace")
    ws = org.workspaces.filter(pk=ws_id).first() if ws_id else None
    if ws_id and ws is None:
        raise ValidationError({"default_workspace": "Unknown workspace."})
    cfg = cfg or SSOConfig(organization=org)
    cfg.issuer, cfg.client_id = issuer, client_id
    cfg.scopes = (d.get("scopes") or "openid email profile").strip()
    cfg.auto_provision = bool(d.get("auto_provision", True))
    cfg.default_org_role = role
    cfg.default_workspace = ws
    cfg.default_workspace_role = d.get("default_workspace_role") or "editor"
    if "client_secret" in d:  # empty string clears, absent keeps
        secrets = dict(cfg.secrets)
        if d["client_secret"]:
            secrets["client_secret"] = d["client_secret"].strip()
        else:
            secrets.pop("client_secret", None)
        cfg.secrets = secrets
    enabled = bool(d.get("enabled", cfg.enabled))
    enforce = bool(d.get("enforce", cfg.enforce))
    if enabled:
        try:
            service.discovery(issuer)
        except service.SSOError as exc:
            raise ValidationError({"issuer": str(exc)})
    verified = cfg.pk and cfg.domains.filter(verified_at__isnull=False).exists()
    if enforce and not (enabled and verified):
        raise ValidationError({"enforce": "Enable SSO and verify at least one domain before enforcing it."})
    cfg.enabled, cfg.enforce = enabled, enforce
    cfg.save()
    return Response(_describe(org, cfg))


@doc(
    "organizations",
    "Claim an email domain for SSO",
    description='Body: {"domain": "acme.com"}. Verify it with the returned DNS TXT record.',
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def add_domain(request, org_id):
    org = _owner_org(request, org_id)
    cfg = SSOConfig.objects.filter(organization=org).first()
    if cfg is None:
        raise ValidationError({"detail": "Save the identity provider settings first."})
    domain = service.normalize_domain(request.data.get("domain"))
    existing = SSODomain.objects.filter(domain=domain).first()
    if existing and existing.config_id != cfg.id:
        raise ValidationError({"domain": "This domain is already claimed by another organization."})
    if existing is None:
        SSODomain.objects.create(config=cfg, domain=domain)
    return Response(_describe(org, cfg), status=status.HTTP_201_CREATED)


@doc("organizations", "Verify a claimed domain via DNS")
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify_domain(request, org_id, domain_id):
    org = _owner_org(request, org_id)
    row = get_object_or_404(SSODomain, pk=domain_id, config__organization=org)
    try:
        service.verify_domain(row)
    except service.SSOError as exc:
        raise ValidationError({"detail": str(exc)})
    return Response(_describe(org, row.config))


@doc("organizations", "Remove a claimed domain")
@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def remove_domain(request, org_id, domain_id):
    org = _owner_org(request, org_id)
    row = get_object_or_404(SSODomain, pk=domain_id, config__organization=org)
    cfg = row.config
    row.delete()
    if cfg.enforce and not cfg.domains.filter(verified_at__isnull=False).exists():
        cfg.enforce = False
        cfg.save(update_fields=["enforce", "updated_at"])
    return Response(_describe(org, cfg))

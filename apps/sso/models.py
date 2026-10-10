import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.fields import EncryptedJSONField
from apps.core.models import TimeStamped
from apps.tenants.models import Organization, OrgRole, Role, Workspace


class SSOConfig(TimeStamped):
    """OpenID Connect single sign-on for one organization, managed by its owners."""

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="sso")
    enabled = models.BooleanField(default=False)
    # e.g. https://accounts.google.com, https://login.microsoftonline.com/<tenant>/v2.0, https://<org>.okta.com
    issuer = models.URLField(max_length=500)
    client_id = models.CharField(max_length=255)
    secrets = EncryptedJSONField(default=dict, blank=True)  # {"client_secret": …}
    scopes = models.CharField(max_length=255, default="openid email profile")
    # Create accounts (and org memberships) on first sign-in for people on the verified domains.
    auto_provision = models.BooleanField(default=True)
    default_org_role = models.CharField(max_length=16, choices=OrgRole.choices, default=OrgRole.MEMBER)
    default_workspace = models.ForeignKey(Workspace, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    default_workspace_role = models.CharField(max_length=16, choices=Role.choices, default=Role.EDITOR)
    # Refuse password sign-in for users on the verified domains.
    enforce = models.BooleanField(default=False)


def _token():
    return secrets.token_urlsafe(24)


class SSODomain(TimeStamped):
    config = models.ForeignKey(SSOConfig, on_delete=models.CASCADE, related_name="domains")
    domain = models.CharField(max_length=253, unique=True)
    verification_token = models.CharField(max_length=64, default=_token)
    verified_at = models.DateTimeField(null=True, blank=True)

    @property
    def txt_name(self):
        return f"_markette-verification.{self.domain}"

    @property
    def txt_value(self):
        return f"markette-verification={self.verification_token}"


def _in_10_minutes():
    return timezone.now() + timedelta(minutes=10)


def _in_2_minutes():
    return timezone.now() + timedelta(minutes=2)


class SSOLoginState(models.Model):
    """Between the redirect to the identity provider and its callback."""

    state = models.CharField(max_length=64, primary_key=True, default=_token)
    config = models.ForeignKey(SSOConfig, on_delete=models.CASCADE)
    code_verifier = models.CharField(max_length=128)
    nonce = models.CharField(max_length=64, default=_token)
    next_path = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField(default=_in_10_minutes)


class SSOTicket(models.Model):
    """One-time code the frontend exchanges for JWTs, so tokens never travel in URLs."""

    code = models.CharField(max_length=64, primary_key=True, default=_token)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    next_path = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField(default=_in_2_minutes)

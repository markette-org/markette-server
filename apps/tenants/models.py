import secrets
import zoneinfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify

from apps.core.models import TimeStamped


def validate_timezone(value):
    if value not in zoneinfo.available_timezones():
        raise ValidationError(f"Unknown time zone: {value}")


def unique_slug(model, name, pk=None, fallback="item"):
    base = slugify(name)[:120] or fallback
    slug, n = base, 1
    while model.objects.filter(slug=slug).exclude(pk=pk).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


class OrgStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"


class Organization(TimeStamped):
    """The customer account: owns workspaces (brands, clients) and carries plan limits."""

    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True)
    status = models.CharField(max_length=16, choices=OrgStatus.choices, default=OrgStatus.ACTIVE)
    # Plan limits; null means unlimited.
    max_workspaces = models.PositiveIntegerField(null=True, blank=True)
    max_channels = models.PositiveIntegerField(null=True, blank=True)
    max_members = models.PositiveIntegerField(null=True, blank=True)
    ai_monthly_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(Organization, self.name, self.pk, "organization")
        super().save(*args, **kwargs)

    @property
    def is_active(self):
        return self.status == OrgStatus.ACTIVE

    def __str__(self):
        return self.name


class OrgRole(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"


class OrgMembership(TimeStamped):
    """Org owners/admins administer every workspace of the org; members only those they are added to."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="org_memberships")
    role = models.CharField(max_length=16, choices=OrgRole.choices, default=OrgRole.MEMBER)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="uniq_org_membership")]

    @property
    def is_admin(self):
        return self.role in (OrgRole.OWNER, OrgRole.ADMIN)


class Workspace(TimeStamped):
    """A brand or client inside an organization. Posts belong to one workspace; channels are shared."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="workspaces")
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    timezone = models.CharField(max_length=64, default="UTC", validators=[validate_timezone])
    # Minimum gap between two posts on the same channel when auto-scheduling.
    min_gap_minutes = models.PositiveIntegerField(default=90)
    ai_enabled = models.BooleanField(default=True)
    # Adult platforms (Pornhub, OnlyFans…) are only offered once an admin confirmed the content rules.
    adult_content = models.BooleanField(default=False)
    adult_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    adult_confirmed_at = models.DateTimeField(null=True, blank=True)
    # UTM tags and short links on social posts: {"utm", "utm_medium", "short_links", "domains"}.
    link_tracking = models.JSONField(default=dict, blank=True)
    # Archived workspaces are read-only and publish nothing.
    archived_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(Workspace, self.name, self.pk, "workspace")
        super().save(*args, **kwargs)

    @property
    def is_live(self):
        return self.archived_at is None and self.organization.is_active

    @property
    def tz(self):
        return zoneinfo.ZoneInfo(self.timezone)

    def __str__(self):
        return self.name


class Role(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    EDITOR = "editor", "Editor"
    VIEWER = "viewer", "Viewer"


ROLE_RANK = {Role.VIEWER: 0, Role.EDITOR: 1, Role.ADMIN: 2, Role.OWNER: 3}


class Membership(TimeStamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.EDITOR)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "user"], name="uniq_membership")]

    def has_role(self, minimum):
        return ROLE_RANK[self.role] >= ROLE_RANK[minimum]


def _invite_token():
    return secrets.token_urlsafe(32)


class Invitation(TimeStamped):
    """Invites to a workspace (``workspace`` + ``role``) or to an organization (``organization`` + ``org_role``)."""

    organization = models.ForeignKey(
        Organization, null=True, blank=True, on_delete=models.CASCADE, related_name="invitations"
    )
    workspace = models.ForeignKey(
        Workspace, null=True, blank=True, on_delete=models.CASCADE, related_name="invitations"
    )
    email = models.EmailField()
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.EDITOR)
    org_role = models.CharField(max_length=16, choices=OrgRole.choices, blank=True)
    token = models.CharField(max_length=64, unique=True, default=_invite_token)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    accepted_at = models.DateTimeField(null=True, blank=True)

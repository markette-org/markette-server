from django.conf import settings
from django.db import models

from apps.channels.models import Channel
from apps.core.models import TimeStamped
from apps.tenants.models import Workspace


class Team(TimeStamped):
    """A group of people who publish to a set of channels, optionally with approval."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    color = models.CharField(max_length=9, default="#6d5efc")
    # Channels assigned to at least one team are visible only to those teams' members
    # (and workspace admins). Unassigned channels stay open to the whole workspace.
    channels = models.ManyToManyField(Channel, blank=True, related_name="teams")
    # Members (not reviewers) must get a reviewer's approval before anything is scheduled.
    requires_approval = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["workspace", "name"], name="uniq_team_name")]

    def __str__(self):
        return self.name


class TeamRole(models.TextChoices):
    MEMBER = "member", "Member"
    REVIEWER = "reviewer", "Reviewer"


class TeamMember(TimeStamped):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_memberships")
    role = models.CharField(max_length=16, choices=TeamRole.choices, default=TeamRole.MEMBER)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["team", "user"], name="uniq_team_member")]

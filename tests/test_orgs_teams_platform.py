from datetime import time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.channels.models import Channel, ConnectionRequest
from apps.channels.services import create_channels
from apps.core.models import User
from apps.insights import ai
from apps.insights.models import AIRequest
from apps.publishing.models import PostingSlot, PostTarget, TargetStatus
from apps.teams.models import Team, TeamMember, TeamRole
from apps.tenants.models import Membership, Organization, OrgMembership, OrgRole, Role, Workspace

pytestmark = pytest.mark.django_db(transaction=True)


def client_for(user, ws=None):
    c = APIClient()
    c.force_authenticate(user)
    if ws:
        c.credentials(HTTP_X_WORKSPACE=ws.slug)
    return c


@pytest.fixture
def editor(workspace):
    u = User.objects.create_user("ed@example.com", "pw-pw-pw-pw-1", name="Ed")
    Membership.objects.create(workspace=workspace, user=u, role=Role.EDITOR)
    return u


@pytest.fixture
def reviewer(workspace):
    u = User.objects.create_user("rita@example.com", "pw-pw-pw-pw-1", name="Rita")
    Membership.objects.create(workspace=workspace, user=u, role=Role.EDITOR)
    return u


# --- organizations ----------------------------------------------------------------


def test_register_creates_organization_owner():
    r = APIClient().post(
        "/api/auth/register/",
        {
            "email": "o@example.com",
            "password": "a-long-passphrase",
            "organization_name": "Acme Agency",
            "workspace_name": "Client A",
        },
        format="json",
    )
    assert r.status_code == 201
    org = Organization.objects.get(name="Acme Agency")
    assert org.memberships.get().role == OrgRole.OWNER
    assert org.workspaces.get().name == "Client A"


def test_org_admin_reaches_every_workspace_members_do_not(workspace):
    org = workspace.organization
    other = Workspace.objects.create(organization=org, name="Client B")
    admin = User.objects.create_user("admin@example.com", "pw-pw-pw-pw-1")
    member = User.objects.create_user("member@example.com", "pw-pw-pw-pw-1")
    OrgMembership.objects.create(organization=org, user=admin, role=OrgRole.ADMIN)
    OrgMembership.objects.create(organization=org, user=member, role=OrgRole.MEMBER)
    slugs = {w["slug"]: w["role"] for w in client_for(admin).get("/api/workspaces/").data["results"]}
    assert slugs == {workspace.slug: "admin", other.slug: "admin"}
    assert client_for(member).get("/api/workspaces/").data["results"] == []
    assert client_for(member, other).get("/api/posts/").status_code == 404
    assert client_for(admin, other).post("/api/posts/", {"body": "hi"}, format="json").status_code == 201


def test_suspended_org_is_locked_and_not_published(api, workspace, channel):
    post = api.post("/api/posts/", {"body": "x", "targets": [{"channel": channel.id}]}, format="json").data
    at = (timezone.now() + timedelta(hours=1)).isoformat()
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "exact", "at": at}, format="json")
    Organization.objects.filter(pk=workspace.organization_id).update(status="suspended")
    assert api.get("/api/posts/").status_code == 403
    PostTarget.objects.update(scheduled_at=timezone.now() - timedelta(minutes=1))
    from apps.publishing.tasks import dispatch_due_targets

    assert dispatch_due_targets() == 0


def test_archived_workspace_is_read_only(api, workspace):
    r = api.patch(f"/api/workspaces/{workspace.id}/", {"archived": True}, format="json")
    assert r.status_code == 200 and r.data["is_archived"] is True
    assert api.get("/api/posts/").status_code == 200
    assert api.post("/api/posts/", {"body": "x"}, format="json").status_code == 403


def test_workspace_and_channel_limits(api, user, workspace, fake_provider):
    org = workspace.organization
    OrgMembership.objects.create(organization=org, user=user, role=OrgRole.OWNER)
    org.max_workspaces, org.max_channels = 1, 1
    org.save()
    r = api.post("/api/workspaces/", {"name": "Second", "organization": org.id}, format="json")
    assert r.status_code == 400 and "1 active workspaces" in str(r.data)
    Channel.create_for(workspace=workspace, provider="fake", external_id="1", name="One")
    conn = ConnectionRequest.objects.create(workspace=workspace, user=user, provider="fake")
    conn.candidates = {"items": [{"external_id": "2", "name": "Two", "provider": "fake"}]}
    conn.save()
    with pytest.raises(Exception, match="1 channels"):
        create_channels(conn, ["2"], user)


def test_org_invitation_flow(user, workspace):
    org = workspace.organization
    OrgMembership.objects.create(organization=org, user=user, role=OrgRole.OWNER)
    owner = client_for(user)
    r = owner.post(
        f"/api/organizations/{org.id}/invitations/", {"email": "new@example.com", "org_role": "admin"}, format="json"
    )
    assert r.status_code == 201
    reg = APIClient().post(
        "/api/auth/register/",
        {"email": "new@example.com", "password": "a-long-passphrase", "invitation": r.data["token"]},
        format="json",
    )
    assert reg.status_code == 201
    assert OrgMembership.objects.get(organization=org, user__email="new@example.com").role == OrgRole.ADMIN


# --- teams: channel access -----------------------------------------------------------


def test_team_restricted_channels(workspace, editor, fake_provider):
    open_ch = Channel.create_for(workspace=workspace, provider="fake", external_id="o", name="Open")
    it_ch = Channel.create_for(workspace=workspace, provider="fake", external_id="it", name="Italia")
    team = Team.objects.create(workspace=workspace, name="Social IT")
    team.channels.add(it_ch)
    c = client_for(editor, workspace)
    names = [ch["name"] for ch in c.get("/api/channels/").data["results"]]
    assert names == ["Open"]
    r = c.post("/api/posts/", {"body": "x", "targets": [{"channel": it_ch.id}]}, format="json")
    assert r.status_code == 400 and "team" in str(r.data)
    TeamMember.objects.create(team=team, user=editor)
    assert len(c.get("/api/channels/").data["results"]) == 2
    assert c.post("/api/posts/", {"body": "x", "targets": [{"channel": it_ch.id}]}, format="json").status_code == 201
    assert open_ch  # unassigned channels stay open


def test_editing_keeps_targets_on_hidden_channels(api, workspace, editor, fake_provider):
    open_ch = Channel.create_for(workspace=workspace, provider="fake", external_id="o", name="Open")
    hidden = Channel.create_for(workspace=workspace, provider="fake", external_id="h", name="Hidden")
    Team.objects.create(workspace=workspace, name="Other").channels.add(hidden)
    post = api.post(
        "/api/posts/", {"body": "x", "targets": [{"channel": open_ch.id}, {"channel": hidden.id}]}, format="json"
    ).data
    c = client_for(editor, workspace)
    seen = c.get(f"/api/posts/{post['id']}/").data
    assert [t["channel"] for t in seen["targets"]] == [open_ch.id] and seen["hidden_targets"] == 1
    r = c.patch(f"/api/posts/{post['id']}/", {"body": "edited", "targets": [{"channel": open_ch.id}]}, format="json")
    assert r.status_code == 200
    assert PostTarget.objects.filter(post_id=post["id"]).count() == 2


# --- teams: approval -------------------------------------------------------------------


@pytest.fixture
def approval_team(workspace, editor, reviewer, fake_provider):
    ch = Channel.create_for(workspace=workspace, provider="fake", external_id="brand", name="Brand")
    team = Team.objects.create(workspace=workspace, name="Brand team", requires_approval=True)
    team.channels.add(ch)
    TeamMember.objects.create(team=team, user=editor, role=TeamRole.MEMBER)
    TeamMember.objects.create(team=team, user=reviewer, role=TeamRole.REVIEWER)
    return team, ch


def test_approval_flow(workspace, editor, reviewer, approval_team):
    _, ch = approval_team
    ed, rv = client_for(editor, workspace), client_for(reviewer, workspace)
    assert ed.get("/api/channels/").data["results"][0]["approval_required"] is True
    post = ed.post("/api/posts/", {"body": "launch!", "targets": [{"channel": ch.id}]}, format="json").data
    at = (timezone.now() + timedelta(hours=3)).replace(microsecond=0)
    r = ed.post(
        f"/api/posts/{post['id']}/schedule/", {"mode": "exact", "at": at.isoformat(), "comment": "pls"}, format="json"
    )
    assert r.status_code == 202 and r.data["approval_status"] == "pending"
    assert r.data["targets"][0]["status"] == "draft"
    # The author cannot sneak it onto the calendar.
    tid = r.data["targets"][0]["id"]
    assert (
        ed.post(f"/api/targets/{tid}/reschedule/", {"scheduled_at": at.isoformat()}, format="json").status_code == 400
    )
    # Reviewer's queue.
    assert [p["id"] for p in rv.get("/api/posts/", {"review": "mine"}).data["results"]] == [post["id"]]
    assert ed.post(f"/api/posts/{post['id']}/approve/", {}, format="json").status_code == 403
    r = rv.post(f"/api/posts/{post['id']}/request-changes/", {"comment": "Add the link"}, format="json")
    assert r.data["approval_status"] == "changes_requested"
    ed.patch(f"/api/posts/{post['id']}/", {"body": "launch! https://x.example"}, format="json")
    assert (
        ed.post(
            f"/api/posts/{post['id']}/schedule/", {"mode": "exact", "at": at.isoformat()}, format="json"
        ).status_code
        == 202
    )
    r = rv.post(f"/api/posts/{post['id']}/approve/", {"comment": "ok"}, format="json")
    assert r.status_code == 200 and r.data["approval_outcome"] == {"scheduled": True}
    t = PostTarget.objects.get(pk=tid)
    assert t.status == TargetStatus.SCHEDULED and t.scheduled_at == at
    actions = [x["action"] for x in ed.get(f"/api/posts/{post['id']}/reviews/").data]
    assert actions == ["submitted", "changes_requested", "submitted", "approved"]
    # Editing an approved post sends it back to draft.
    ed.patch(f"/api/posts/{post['id']}/", {"body": "changed after approval"}, format="json")
    t.refresh_from_db()
    assert t.status == TargetStatus.DRAFT
    assert ed.get(f"/api/posts/{post['id']}/").data["approval_status"] == ""


def test_reviewers_and_admins_skip_approval(api, workspace, reviewer, approval_team):
    _, ch = approval_team
    for c in (client_for(reviewer, workspace), api):
        post = c.post("/api/posts/", {"body": "direct", "targets": [{"channel": ch.id}]}, format="json").data
        r = c.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
        assert r.status_code == 200 and r.data["status"] == "published"


def test_team_queue_slots(api, workspace, channel):
    team = Team.objects.create(workspace=workspace, name="Night shift")
    team.channels.add(channel)
    PostingSlot.objects.create(workspace=workspace, weekday=0, time=time(9, 0))
    for wd in range(7):
        PostingSlot.objects.create(workspace=workspace, team=team, weekday=wd, time=time(22, 30))
    post = api.post("/api/posts/", {"body": "x", "targets": [{"channel": channel.id}]}, format="json").data
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "queue"}, format="json")
    local = PostTarget.objects.get(post_id=post["id"]).scheduled_at.astimezone(workspace.tz)
    assert (local.hour, local.minute) == (22, 30)


# --- AI quota --------------------------------------------------------------------------------


def test_ai_quota_blocks_calls(workspace, user, settings):
    settings.AI_ENABLED = True
    org = workspace.organization
    org.ai_monthly_tokens = 1000
    org.save()
    AIRequest.objects.create(
        workspace=workspace, kind="adapt_content", model="m", prompt="", input_tokens=900, output_tokens=200
    )
    with pytest.raises(ai.AIUnavailable, match="allowance"):
        ai._call("adapt_content", workspace, user, "hi", ai.Adaptation)


# --- platform ---------------------------------------------------------------------------------


def test_platform_requires_superadmin(api):
    assert api.get("/api/platform/overview/").status_code == 403


def test_platform_console(user, workspace, channel):
    root = User.objects.create_superuser("root@example.com", "pw-pw-pw-pw-1")
    c = client_for(root)
    ov = c.get("/api/platform/overview/").data
    assert ov["organizations"]["total"] == 1 and ov["channels"]["by_provider"] == {"fake": 1}
    r = c.post(
        "/api/platform/organizations/",
        {"name": "New Client", "owner_email": user.email, "max_channels": 5},
        format="json",
    )
    assert r.status_code == 201 and r.data["owners"] == [user.email]
    r = c.post("/api/platform/organizations/", {"name": "Prospect", "owner_email": "later@example.com"}, format="json")
    detail = c.get(f"/api/platform/organizations/{r.data['id']}/detail/").data
    assert detail["invitations"][0]["org_role"] == "owner"
    org_id = workspace.organization_id
    assert c.patch(f"/api/platform/organizations/{org_id}/", {"status": "suspended"}, format="json").status_code == 200
    assert client_for(user, workspace).get("/api/posts/").status_code == 403
    users = c.get("/api/platform/users/", {"search": "ada"}).data["results"]
    assert users[0]["email"] == user.email
    pw = c.post(f"/api/platform/users/{user.id}/set-password/").data["temporary_password"]
    user.refresh_from_db()
    assert user.check_password(pw)
    assert c.patch(f"/api/platform/users/{root.id}/", {"is_active": False}, format="json").status_code == 400
    assert c.get("/api/platform/health/").data["status"] == "ok"
    assert c.get("/api/platform/ai-usage/", {"month": "2026-10"}).status_code == 200
    assert client_for(user).get("/api/auth/me/").data["is_superadmin"] is False


def test_user_can_own_several_organizations(user, workspace):
    from apps.platform.models import PlatformSettings

    c = client_for(user)
    r = c.post("/api/organizations/", {"name": "Side Project", "timezone": "Europe/Rome"}, format="json")
    assert r.status_code == 201
    assert r.data["organization"]["role"] == "owner" and r.data["workspace"]["role"] == "owner"
    r2 = c.post("/api/organizations/", {"name": "Client Co", "workspace_name": "Launch"}, format="json")
    orgs = {o["name"] for o in c.get("/api/organizations/").data["results"]}
    assert {"Side Project", "Client Co", workspace.organization.name} <= orgs
    names = {w["name"] for w in c.get("/api/workspaces/").data["results"]}
    assert {"Side Project", "Launch", workspace.name} <= names
    # Last owner cannot leave; after adding another owner they can.
    org_id = r2.data["organization"]["id"]
    assert c.post(f"/api/organizations/{org_id}/leave/").status_code == 400
    other = User.objects.create_user("co@example.com", "pw-pw-pw-pw-1")
    OrgMembership.objects.create(organization_id=org_id, user=other, role=OrgRole.OWNER)
    assert c.post(f"/api/organizations/{org_id}/leave/").status_code == 204
    assert "Launch" not in {w["name"] for w in c.get("/api/workspaces/").data["results"]}
    ps = PlatformSettings.load()
    ps.org_creation_open = False
    ps.save()
    assert c.post("/api/organizations/", {"name": "Nope"}, format="json").status_code == 403

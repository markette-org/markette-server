import pytest
from rest_framework.test import APIClient

from apps.channels.models import Channel, ChannelAccess, ConnectionRequest
from apps.channels.services import create_channels
from apps.core.models import User
from apps.publishing.models import PostTarget
from apps.teams.models import Team
from apps.tenants.models import Membership, OrgMembership, OrgRole, Role, Workspace

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def org_admin(user, workspace):
    OrgMembership.objects.create(organization=workspace.organization, user=user, role=OrgRole.OWNER)
    return user


@pytest.fixture
def second_ws(workspace):
    return Workspace.objects.create(organization=workspace.organization, name="Brand B", timezone="Europe/Rome")


def client(user, ws=None):
    c = APIClient()
    c.force_authenticate(user)
    if ws:
        c.credentials(HTTP_X_WORKSPACE=ws.slug)
    return c


def test_org_channels_are_available_everywhere_relevant_first(org_admin, workspace, second_ws, channel):
    other = Channel.create_for(workspace=second_ws, provider="fake", external_id="b", name="Brand B account")
    b = client(org_admin, second_ws)
    rows = b.get("/api/channels/").data["results"]
    assert [(r["name"], r["relevant"]) for r in rows] == [("Brand B account", True), ("Fake One", False)]
    # Usable from Brand B even though it was connected in Acme.
    assert (
        b.post("/api/posts/", {"body": "news", "targets": [{"channel": channel.id}]}, format="json").status_code == 201
    )
    assert [r["name"] for r in b.get("/api/channels/", {"relevant": "1"}).data["results"]] == ["Brand B account"]
    r = b.post(f"/api/channels/{channel.id}/relevance/", {"relevant": True}, format="json")
    assert r.data["relevant"] is True
    assert {w["name"] for w in r.data["workspaces"]} == {"Acme", "Brand B"}
    assert other.organization_id == channel.organization_id


def test_org_admin_sets_relevance(org_admin, workspace, second_ws, channel):
    c = client(org_admin)
    org = workspace.organization
    r = c.patch(f"/api/organizations/{org.id}/channels/{channel.id}/", {"workspaces": [second_ws.id]}, format="json")
    assert [w["name"] for w in r.data["workspaces"]] == ["Brand B"]
    r = c.patch(f"/api/organizations/{org.id}/channels/{channel.id}/", {"workspaces": []}, format="json")
    assert r.data["workspaces"] == []
    assert client(org_admin, workspace).get("/api/channels/").data["results"][0]["name"] == "Fake One"


def test_best_time_respects_gap_across_workspaces(org_admin, workspace, second_ws, channel):
    for ws in (workspace, second_ws):
        ws.min_gap_minutes = 240
        ws.save()
    times = []
    for ws in (workspace, second_ws):
        wc = client(org_admin, ws)
        post = wc.post("/api/posts/", {"body": ws.name, "targets": [{"channel": channel.id}]}, format="json").data
        wc.post(f"/api/posts/{post['id']}/schedule/", {"mode": "best_time"}, format="json")
        times.append(PostTarget.objects.get(post_id=post["id"]).scheduled_at)
    assert abs((times[0] - times[1]).total_seconds()) >= 240 * 60


def test_disconnecting_is_for_org_admins(org_admin, workspace, second_ws, channel):
    ws_owner = User.objects.create_user("wso@example.com", "pw-pw-pw-pw-1")
    Membership.objects.create(workspace=workspace, user=ws_owner, role=Role.OWNER)
    r = client(ws_owner, workspace).delete(f"/api/channels/{channel.id}/")
    assert r.status_code == 403 and "organization admin" in str(r.data)
    assert client(org_admin, workspace).delete(f"/api/channels/{channel.id}/").status_code == 204
    assert not Channel.objects.filter(pk=channel.pk).exists()


def test_team_rules_are_per_workspace(workspace, second_ws, channel):
    editor = User.objects.create_user("ed@example.com", "pw-pw-pw-pw-1")
    for ws in (workspace, second_ws):
        Membership.objects.create(workspace=ws, user=editor, role=Role.EDITOR)
    Team.objects.create(workspace=workspace, name="Restricted").channels.add(channel)
    assert client(editor, workspace).get("/api/channels/").data["results"] == []
    assert len(client(editor, second_ws).get("/api/channels/").data["results"]) == 1


def test_connecting_same_account_reuses_the_org_channel(org_admin, workspace, second_ws, channel, fake_provider):
    conn = ConnectionRequest.objects.create(workspace=second_ws, user=org_admin, provider="fake")
    conn.candidates = {"items": [{"external_id": "1", "name": "Fake One", "provider": "fake"}]}
    conn.save()
    [again] = create_channels(conn, ["1"], org_admin)
    assert again.pk == channel.pk
    # Reconnecting from Brand B marks the existing account as relevant there too.
    assert set(again.workspaces.values_list("id", flat=True)) == {workspace.id, second_ws.id}


def test_only_org_admins_manage_sharing(workspace, channel):
    editor = User.objects.create_user("e2@example.com", "pw-pw-pw-pw-1")
    OrgMembership.objects.create(organization=workspace.organization, user=editor, role=OrgRole.MEMBER)
    assert client(editor).get(f"/api/organizations/{workspace.organization_id}/channels/").status_code == 403


def test_relevant_workspace_names_hidden_from_non_members(workspace, second_ws, channel):
    ChannelAccess.objects.create(channel=channel, workspace=second_ws)
    editor = User.objects.create_user("only-a@example.com", "pw-pw-pw-pw-1")
    Membership.objects.create(workspace=workspace, user=editor, role=Role.EDITOR)
    data = client(editor, workspace).get("/api/channels/").data["results"][0]
    assert [w["name"] for w in data["workspaces"]] == ["Acme"] and data["shared_count"] == 2
    assert data["organization"] == workspace.organization_id

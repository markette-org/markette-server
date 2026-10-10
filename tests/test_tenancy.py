from rest_framework.test import APIClient

from apps.core.models import User
from apps.publishing.models import Post
from apps.tenants.models import Membership, Organization, Role, Workspace


def test_register_creates_workspace_and_tokens(db):
    r = APIClient().post(
        "/api/auth/register/",
        {
            "email": "New@Example.com",
            "password": "a-long-passphrase",
            "workspace_name": "Studio",
            "timezone": "Europe/Rome",
        },
        format="json",
    )
    assert r.status_code == 201
    assert r.data["access"] and r.data["user"]["email"] == "new@example.com"
    ws = Workspace.objects.get(name="Studio")
    assert ws.memberships.get().role == Role.OWNER


def test_workspace_header_is_required(api):
    api.credentials()
    assert api.get("/api/posts/").status_code == 400


def test_cannot_read_other_tenants_data(api, workspace):
    other_ws = Workspace.objects.create(organization=Organization.objects.create(name="Other"), name="Other")
    Post.objects.create(workspace=other_ws, body="secret")
    Post.objects.create(workspace=workspace, body="mine")
    bodies = [p["body"] for p in api.get("/api/posts/").data["results"]]
    assert bodies == ["mine"]
    api.credentials(HTTP_X_WORKSPACE=other_ws.slug)
    assert api.get("/api/posts/").status_code == 404


def test_viewer_cannot_write(db, workspace):
    viewer = User.objects.create_user("v@example.com", "pw-pw-pw-pw")
    Membership.objects.create(workspace=workspace, user=viewer, role=Role.VIEWER)
    c = APIClient()
    c.force_authenticate(viewer)
    c.credentials(HTTP_X_WORKSPACE=workspace.slug)
    assert c.get("/api/posts/").status_code == 200
    assert c.post("/api/posts/", {"body": "x"}, format="json").status_code == 403


def test_cannot_target_channel_of_other_workspace(api, fake_provider):
    from apps.channels.models import Channel

    foreign = Channel.create_for(
        workspace=Workspace.objects.create(organization=Organization.objects.create(name="B"), name="B"),
        provider="fake",
        external_id="x",
        name="Foreign",
    )
    r = api.post("/api/posts/", {"body": "hi", "targets": [{"channel": foreign.id}]}, format="json")
    assert r.status_code == 400


def test_status_filter_accepts_several_values(api, workspace):
    for st in ("failed", "partial", "published"):
        Post.objects.create(workspace=workspace, body=st, status=st)
    r = api.get("/api/posts/", {"status": "failed,partial"})
    assert sorted(p["status"] for p in r.data["results"]) == ["failed", "partial"]

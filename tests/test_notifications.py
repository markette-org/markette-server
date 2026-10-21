import hashlib
import hmac
import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from apps.channels.models import Channel, ChannelStatus
from apps.channels.providers import ProviderError
from apps.core.models import User
from apps.notifications import tasks as ntasks
from apps.notifications.models import DISABLE_AFTER, Delivery, Destination, Event
from apps.publishing.models import PostTarget, TargetStatus
from apps.tenants.models import Membership, Role

pytestmark = pytest.mark.django_db(transaction=True)

HOOK = "https://hooks.example/markette"
TOKEN = "123456:" + "A" * 35


def publish(api, channel, body="Hello world"):
    post = api.post("/api/posts/", {"body": body, "targets": [{"channel": channel.id}]}, format="json").data
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    return post


def webhook(api, events=None):
    r = api.post(
        "/api/notifications/",
        {"kind": "webhook", "name": "n8n", "config": {"url": HOOK}, **({"events": events} if events else {})},
        format="json",
    )
    assert r.status_code == 201, r.data
    return r.data


@respx.mock
def test_webhook_is_signed_and_carries_the_publication(api, channel, fake_provider):
    created = webhook(api, events=["publication.published", "post.published", "publication.failed"])
    secret = created["signing_secret"]
    assert secret.startswith("whsec_")
    # The secret is shown once; listings never return it.
    listed = api.get("/api/notifications/").data[0]
    assert "signing_secret" not in listed and "secrets" not in listed
    route = respx.post(HOOK).mock(return_value=httpx.Response(204))
    post = publish(api, channel)
    events = [json.loads(c.request.content)["event"] for c in route.calls]
    assert sorted(events) == ["post.published", "publication.published"]
    req = next(c.request for c in route.calls if c.request.headers["x-markette-event"] == "publication.published")
    body = req.content
    ts = req.headers["x-markette-timestamp"]
    expected = "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert req.headers["x-markette-signature"] == expected
    payload = json.loads(body)
    assert payload["id"] == req.headers["x-markette-delivery"]
    assert payload["data"]["post"]["id"] == post["id"]
    assert payload["data"]["publication"]["url"] == "https://fake.example/fake-1"
    assert payload["data"]["publication"]["channel"]["name"] == "Fake One"
    assert Delivery.objects.filter(status="sent").count() == 2


@respx.mock
def test_telegram_status_bot_reports_failures(api, channel, fake_provider):
    r = api.post(
        "/api/notifications/",
        {"kind": "telegram", "name": "Status", "config": {"chat_id": "-1001"}, "bot_token": TOKEN},
        format="json",
    )
    assert r.status_code == 201, r.data
    assert (
        r.data["has_bot_token"]
        and "bot_token" not in r.data
        and r.data["events"]
        == sorted(
            [
                "publication.failed",
                "publication.unconfirmed",
                "publication.handoff",
                "channel.needs_reconnect",
                "approval.requested",
            ]
        )
    )
    send = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    fake_provider.fail_with = ProviderError("Bad <request> & co")
    publish(api, channel, body="Launch day")
    msg = json.loads(send.calls.last.request.content)
    assert msg["chat_id"] == -1001 and msg["parse_mode"] == "HTML"
    assert "Publishing on Fake One failed" in msg["text"] and "Launch day" in msg["text"]
    assert "Bad &lt;request&gt; &amp; co" in msg["text"]  # escaped for Telegram's HTML


def test_email_goes_to_admins_when_nobody_is_listed(api, channel, fake_provider, workspace, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    viewer = User.objects.create_user("viewer@example.com", "x")
    Membership.objects.create(workspace=workspace, user=viewer, role=Role.VIEWER)
    r = api.post("/api/notifications/", {"kind": "email", "name": "Team"}, format="json")
    assert r.status_code == 201, r.data
    fake_provider.fail_with = ProviderError("nope")
    publish(api, channel)
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["ada@example.com"] and "failed" in mail.outbox[0].subject
    bad = api.post("/api/notifications/", {"kind": "email", "name": "x", "config": {"to": ["nope"]}}, format="json")
    assert bad.status_code == 400


@respx.mock
def test_retries_then_switches_off_a_broken_destination(api, channel, fake_provider):
    created = webhook(api, events=["publication.published"])
    respx.post(HOOK).mock(return_value=httpx.Response(503))
    publish(api, channel)
    d = Delivery.objects.get()
    assert d.status == "pending" and d.attempts == 1 and d.next_attempt_at
    # A permanent error (4xx) is not retried.
    respx.post(HOOK).mock(return_value=httpx.Response(410))
    Delivery.objects.update(next_attempt_at=timezone.now())
    ntasks.retry_due_deliveries()
    d.refresh_from_db()
    assert d.status == "failed" and d.attempts == 2 and d.response_status == 410
    Destination.objects.filter(pk=created["id"]).update(failures=DISABLE_AFTER - 1)
    publish(api, channel)
    dest = Destination.objects.get(pk=created["id"])
    assert not dest.enabled and "Switched off" in dest.last_error


def test_only_admins_manage_destinations(workspace, channel):
    editor = User.objects.create_user("ed@example.com", "x")
    Membership.objects.create(workspace=workspace, user=editor, role=Role.EDITOR)
    c = APIClient()
    c.force_authenticate(editor)
    c.credentials(HTTP_X_WORKSPACE=workspace.slug)
    assert c.get("/api/notifications/").status_code == 403


@respx.mock
def test_reconnect_is_announced_once(api, channel):
    created = webhook(api, events=["channel.needs_reconnect"])
    route = respx.post(HOOK).mock(return_value=httpx.Response(200))
    channel.mark_needs_reauth("token revoked")
    channel.mark_needs_reauth("token revoked again")
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content)["data"]["channel"]["error"] == "token revoked"
    assert Channel.objects.get(pk=channel.pk).status == ChannelStatus.NEEDS_REAUTH
    assert created


@respx.mock
def test_test_action_and_telegram_chat_discovery(api):
    created = webhook(api)
    respx.post(HOOK).mock(return_value=httpx.Response(500, text="boom"))
    r = api.post(f"/api/notifications/{created['id']}/test/")
    assert r.data["status"] == "pending" and "500" in r.data["error"]
    assert api.get(f"/api/notifications/{created['id']}/deliveries/").data[0]["event"] == "test"
    respx.post(f"https://api.telegram.org/bot{TOKEN}/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "markette_status_bot"}})
    )
    respx.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"message": {"chat": {"id": 42, "type": "private", "first_name": "Ada"}}},
                    {"my_chat_member": {"chat": {"id": -1001, "type": "channel", "title": "Ops"}}},
                ],
            },
        )
    )
    r = api.post("/api/notifications/telegram-chats/", {"bot_token": TOKEN}, format="json")
    assert r.data["bot"] == "markette_status_bot"
    assert {c["title"] for c in r.data["chats"]} == {"Ada", "Ops"}


@respx.mock
def test_daily_digest_at_eight_local_time(api, workspace, channel, monkeypatch):
    from apps.publishing.models import Post

    webhook(api, events=["digest.daily"])
    route = respx.post(HOOK).mock(return_value=httpx.Response(200))
    eight = datetime(2026, 10, 12, 8, 5, tzinfo=workspace.tz)  # Europe/Rome
    post = Post.objects.create(workspace=workspace, body="Morning news")
    PostTarget.objects.create(
        post=post, channel=channel, status=TargetStatus.SCHEDULED, scheduled_at=eight + timedelta(hours=2)
    )
    monkeypatch.setattr(timezone, "now", lambda: eight)
    assert ntasks.send_daily_digests() == 1
    assert ntasks.send_daily_digests() == 0  # once a day
    data = json.loads(route.calls.last.request.content)["data"]
    assert data["date"] == "2026-10-12" and data["scheduled_today"] == 1
    assert "10:05 Fake One: Morning news" in data["lines"]


@respx.mock
def test_approval_request_is_announced(workspace, api, fake_provider):
    from apps.teams.models import Team, TeamMember, TeamRole

    webhook(api, events=["approval.requested", "approval.approved"])
    route = respx.post(HOOK).mock(return_value=httpx.Response(200))
    editor = User.objects.create_user("ed@example.com", "x", name="Ed")
    Membership.objects.create(workspace=workspace, user=editor, role=Role.EDITOR)
    ch = Channel.create_for(workspace=workspace, provider="fake", external_id="brand", name="Brand")
    team = Team.objects.create(workspace=workspace, name="Brand team", requires_approval=True)
    team.channels.add(ch)
    TeamMember.objects.create(team=team, user=editor, role=TeamRole.MEMBER)
    c = APIClient()
    c.force_authenticate(editor)
    c.credentials(HTTP_X_WORKSPACE=workspace.slug)
    post = c.post("/api/posts/", {"body": "launch!", "targets": [{"channel": ch.id}]}, format="json").data
    at = (timezone.now() + timedelta(hours=3)).isoformat()
    assert c.post(f"/api/posts/{post['id']}/schedule/", {"mode": "exact", "at": at}, format="json").status_code == 202
    assert api.post(f"/api/posts/{post['id']}/approve/", {}, format="json").status_code == 200
    events = [json.loads(x.request.content) for x in route.calls]
    assert [e["event"] for e in events] == [Event.APPROVAL_REQUESTED, Event.APPROVED]
    assert events[0]["data"]["by"] == "Ed"

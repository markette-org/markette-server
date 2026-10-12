import csv
import io
import json
from datetime import date, datetime, timedelta

import httpx
import pytest
import respx
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from apps.analytics import tasks as atasks
from apps.analytics.models import Report
from apps.analytics.reports import due, period_range
from apps.publishing.models import Post, PostTarget, TargetStatus

pytestmark = pytest.mark.django_db(transaction=True)


def draft(api, channel, body="Launch post"):
    return api.post("/api/posts/", {"body": body, "targets": [{"channel": channel.id}]}, format="json").data


def token_of(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


@respx.mock
def test_client_approves_through_a_link(api, channel, fake_provider):
    hook = "https://hooks.example/n"
    api.post(
        "/api/notifications/",
        {"kind": "webhook", "name": "h", "config": {"url": hook}, "events": ["approval.approved"]},
        format="json",
    )
    route = respx.post(hook).mock(return_value=httpx.Response(200))
    post = draft(api, channel)
    links = api.post(f"/api/posts/{post['id']}/review-links/", {"label": "Acme", "days": 7}, format="json").data
    assert links[0]["active"] and links[0]["url"].endswith(f"/review/{token_of(links[0]['url'])}")
    anon = APIClient()
    token = token_of(links[0]["url"])
    page = anon.get(f"/api/public/review/{token}/").data
    assert page["post"]["body"] == "Launch post" and page["channels"][0]["name"] == "Fake One"
    assert "email" not in json.dumps(page, default=str)
    r = anon.post(f"/api/public/review/{token}/", {"decision": "approve", "name": "Jo"}, format="json")
    assert r.status_code == 200 and r.data["post"]["approval_status"] == "approved"
    reviews = api.get(f"/api/posts/{post['id']}/reviews/").data
    assert reviews[-1]["user"] == "Jo" and reviews[-1]["via_link"]
    assert json.loads(route.calls.last.request.content)["data"]["by"] == "Jo (review link)"


def test_client_changes_unschedule_and_links_expire(api, channel, fake_provider):
    post = draft(api, channel)
    at = (timezone.now() + timedelta(days=2)).isoformat()
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "exact", "at": at}, format="json")
    link = api.post(f"/api/posts/{post['id']}/review-links/", {}, format="json").data[0]
    token = token_of(link["url"])
    anon = APIClient()
    assert anon.post(f"/api/public/review/{token}/", {"decision": "changes"}, format="json").status_code == 400
    r = anon.post(f"/api/public/review/{token}/", {"decision": "changes", "comment": "Use the new logo"}, format="json")
    assert r.status_code == 200
    t = PostTarget.objects.get(post_id=post["id"])
    assert t.status == TargetStatus.DRAFT and t.scheduled_at is None
    api.post(f"/api/posts/{post['id']}/review-links/{link['id']}/revoke/")
    assert anon.get(f"/api/public/review/{token}/").status_code == 404
    assert anon.get("/api/public/review/not-a-token/").status_code == 404


def published(workspace, channel, when, **metrics):
    post = Post.objects.create(workspace=workspace, body="Hello")
    return PostTarget.objects.create(
        post=post,
        channel=channel,
        status=TargetStatus.PUBLISHED,
        published_at=when,
        external_url="https://fake.example/1",
        metrics={"engagements": 5, "likes": 5, **metrics},
        link_clicks=3,
    )


def test_periods_and_schedule():
    r = Report(period="previous_month")
    assert period_range(r, date(2026, 10, 12)) == (date(2026, 9, 1), date(2026, 9, 30))
    r.period = "previous_week"
    assert period_range(r, date(2026, 10, 12)) == (date(2026, 10, 5), date(2026, 10, 11))
    tz = timezone.get_current_timezone()
    r.frequency = "weekly"
    assert due(r, datetime(2026, 10, 12, 8, tzinfo=tz))  # a Monday
    assert not due(r, datetime(2026, 10, 13, 8, tzinfo=tz))
    r.frequency = "monthly"
    assert due(r, datetime(2026, 11, 1, 8, tzinfo=tz)) and not due(r, datetime(2026, 11, 1, 9, tzinfo=tz))


def test_report_email_share_and_csv(api, workspace, channel, settings, monkeypatch):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    monday = datetime(2026, 10, 12, 8, 2, tzinfo=workspace.tz)
    published(workspace, channel, monday - timedelta(days=3))
    published(workspace, channel, monday - timedelta(days=20))  # outside the previous week
    r = api.post(
        "/api/reports/",
        {"name": "Weekly", "period": "previous_week", "frequency": "weekly", "recipients": ["boss@acme.org"]},
        format="json",
    )
    assert r.status_code == 201, r.data
    assert api.post("/api/reports/", {"name": "x", "frequency": "weekly"}, format="json").status_code == 400
    monkeypatch.setattr(timezone, "now", lambda: monday)
    assert atasks.send_due_reports() == 1 and atasks.send_due_reports() == 0
    msg = mail.outbox[0]
    assert msg.to == ["boss@acme.org"] and "Weekly: 2026-10-05 – 2026-10-11" in msg.subject
    name, content, mime = msg.attachments[0]
    rows = list(csv.DictReader(io.StringIO(content.decode() if isinstance(content, bytes) else content)))
    assert len(rows) == 1 and rows[0]["link_clicks"] == "3" and rows[0]["channel"] == "Fake One"
    shared = api.post(f"/api/reports/{r.data['id']}/share/").data["share_url"]
    anon = APIClient()
    page = anon.get(f"/api/public/reports/{token_of(shared)}/").data
    assert page["totals"]["posts"] == 1 and page["totals"]["link_clicks"] == 3 and "target" not in page["top_posts"][0]
    download = anon.get(f"/api/public/reports/{token_of(shared)}/", {"download": "csv"})
    assert download["Content-Type"] == "text/csv" and b"Fake One" in download.content
    api.delete(f"/api/reports/{r.data['id']}/share/")
    assert anon.get(f"/api/public/reports/{token_of(shared)}/").status_code == 404


def test_analytics_csv_export(api, workspace, channel):
    published(workspace, channel, timezone.now() - timedelta(days=1))
    resp = api.get("/api/analytics/export/")
    assert resp.status_code == 200 and resp["Content-Type"] == "text/csv"
    assert b"published_at,channel" in resp.content and b"Fake One" in resp.content

from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.analytics.tasks import collect_due_metrics
from apps.channels.models import ChannelStatus
from apps.channels.providers import ProviderError
from apps.publishing.models import PostingSlot, PostTarget, TargetStatus
from apps.publishing.tasks import dispatch_due_targets, requeue_stuck_targets

# on_commit hooks (dispatching to workers) only fire in real transactions.
pytestmark = pytest.mark.django_db(transaction=True)


def make_post(api, channel, body="Hello world", **extra):
    r = api.post("/api/posts/", {"body": body, "targets": [{"channel": channel.id}], **extra}, format="json")
    assert r.status_code == 201, r.data
    return r.data


def test_publish_now_end_to_end(api, channel, fake_provider):
    post = make_post(api, channel)
    r = api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    assert r.status_code == 200, r.data
    t = PostTarget.objects.get(post_id=post["id"])
    assert t.status == TargetStatus.PUBLISHED
    assert t.external_url == "https://fake.example/fake-1"
    assert t.next_metrics_at is not None
    assert fake_provider.published[0][1].text == "Hello world"
    assert api.get(f"/api/posts/{post['id']}/").data["status"] == "published"


def test_exact_schedule_waits_until_due(api, channel, fake_provider, time_machine=None):
    post = make_post(api, channel)
    at = timezone.now() + timedelta(hours=2)
    r = api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "exact", "at": at.isoformat()}, format="json")
    assert r.status_code == 200, r.data
    assert dispatch_due_targets() == 0
    PostTarget.objects.filter(post_id=post["id"]).update(scheduled_at=timezone.now() - timedelta(seconds=1))
    assert dispatch_due_targets() == 1
    assert PostTarget.objects.get(post_id=post["id"]).status == TargetStatus.PUBLISHED


def test_per_channel_override_text(api, channel, fake_provider):
    r = api.post(
        "/api/posts/", {"body": "Generic", "targets": [{"channel": channel.id, "body": "Tailored"}]}, format="json"
    )
    api.post(f"/api/posts/{r.data['id']}/schedule/", {"mode": "now"}, format="json")
    assert fake_provider.published[0][1].text == "Tailored"


def test_validation_blocks_too_long_text(api, channel):
    post = make_post(api, channel, body="x" * 300)
    r = api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    assert r.status_code == 400
    assert "280" in str(r.data)


def test_retryable_error_backs_off_then_succeeds(api, channel, fake_provider):
    fake_provider.fail_with = ProviderError("rate limited", retryable=True, retry_after=120)
    post = make_post(api, channel)
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    t = PostTarget.objects.get(post_id=post["id"])
    assert t.status == TargetStatus.SCHEDULED and t.attempts == 1
    assert t.next_attempt_at > timezone.now() + timedelta(seconds=100)
    assert dispatch_due_targets() == 0  # still backing off
    fake_provider.fail_with = None
    PostTarget.objects.filter(pk=t.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    dispatch_due_targets()
    t.refresh_from_db()
    assert t.status == TargetStatus.PUBLISHED and t.attempts == 2


def test_gives_up_after_max_attempts(api, channel, fake_provider, settings):
    settings.PUBLISH_MAX_ATTEMPTS = 2
    fake_provider.fail_with = ProviderError("boom", retryable=True)
    post = make_post(api, channel)
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    PostTarget.objects.filter(post_id=post["id"]).update(next_attempt_at=None)
    dispatch_due_targets()
    assert PostTarget.objects.get(post_id=post["id"]).status == TargetStatus.FAILED


def test_auth_error_flags_channel(api, channel, fake_provider):
    fake_provider.fail_with = ProviderError("token revoked", auth=True)
    post = make_post(api, channel)
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    channel.refresh_from_db()
    assert channel.status == ChannelStatus.NEEDS_REAUTH
    assert PostTarget.objects.get(post_id=post["id"]).status == TargetStatus.FAILED


def test_interrupted_publish_is_not_retried_blindly(api, channel):
    post = make_post(api, channel)
    PostTarget.objects.filter(post_id=post["id"]).update(status=TargetStatus.PUBLISHING)
    PostTarget.objects.filter(post_id=post["id"]).update(updated_at=timezone.now() - timedelta(hours=1))
    requeue_stuck_targets()
    t = PostTarget.objects.get(post_id=post["id"])
    assert t.status == TargetStatus.UNCONFIRMED and "It may be live" in t.last_error


def test_queue_mode_uses_next_free_slot(api, channel, workspace):
    for wd in range(7):
        PostingSlot.objects.create(workspace=workspace, weekday=wd, time=time(9, 0))
    p1, p2 = make_post(api, channel), make_post(api, channel, body="second")
    api.post(f"/api/posts/{p1['id']}/schedule/", {"mode": "queue"}, format="json")
    api.post(f"/api/posts/{p2['id']}/schedule/", {"mode": "queue"}, format="json")
    t1 = PostTarget.objects.get(post_id=p1["id"]).scheduled_at.astimezone(workspace.tz)
    t2 = PostTarget.objects.get(post_id=p2["id"]).scheduled_at.astimezone(workspace.tz)
    assert t1.hour == 9 and t2.hour == 9
    assert (t2.date() - t1.date()).days == 1


def test_best_time_mode_respects_gap(api, channel, workspace):
    workspace.min_gap_minutes = 180
    workspace.save()
    p1, p2 = make_post(api, channel), make_post(api, channel, body="second")
    for p in (p1, p2):
        r = api.post(f"/api/posts/{p['id']}/schedule/", {"mode": "best_time"}, format="json")
        assert r.status_code == 200, r.data
    a, b = (PostTarget.objects.get(post_id=p["id"]).scheduled_at for p in (p1, p2))
    assert abs((a - b).total_seconds()) >= 180 * 60
    assert "Best time" in PostTarget.objects.get(post_id=p1["id"]).schedule_reason


def test_calendar_and_reschedule(api, channel):
    post = make_post(api, channel)
    at = timezone.now() + timedelta(days=1)
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "exact", "at": at.isoformat()}, format="json")
    start, end = (timezone.now() - timedelta(days=1)).isoformat(), (timezone.now() + timedelta(days=3)).isoformat()
    items = api.get("/api/calendar/", {"start": start, "end": end}).data
    assert len(items) == 1 and items[0]["channel_name"] == "Fake One"
    new = (at + timedelta(hours=5)).isoformat()
    r = api.post(f"/api/targets/{items[0]['id']}/reschedule/", {"scheduled_at": new}, format="json")
    assert r.status_code == 200, r.data
    assert r.data["schedule_reason"] == "Moved on calendar"


def test_metrics_collected_and_aggregated(api, channel, fake_provider):
    post = make_post(api, channel)
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    PostTarget.objects.filter(post_id=post["id"]).update(next_metrics_at=timezone.now() - timedelta(minutes=1))
    assert collect_due_metrics() == 1
    t = PostTarget.objects.get(post_id=post["id"])
    assert t.metrics["likes"] == 7 and t.metrics["engagement_rate"] == pytest.approx(0.07)
    assert t.next_metrics_at > timezone.now()
    ov = api.get("/api/analytics/overview/").data
    assert ov["totals"]["likes"] == 7 and ov["totals"]["posts"] == 1
    assert ov["top_posts"][0]["target"] == t.id

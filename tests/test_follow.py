"""Publications that wait for another one and link to it (e.g. announce a blog post on X)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.channels.models import Channel
from apps.channels.providers import ProviderError
from apps.publishing.models import Post, PostTarget, TargetStatus
from apps.publishing.tasks import dispatch_due_targets
from apps.tenants.models import Organization, Workspace

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def blog(workspace, fake_provider):
    return Channel.create_for(workspace=workspace, provider="fake", external_id="blog", name="Blog", credentials={})


@pytest.fixture
def social(workspace, fake_provider):
    return Channel.create_for(workspace=workspace, provider="fake", external_id="x", name="Social", credentials={})


def create(api, body, targets, **extra):
    r = api.post("/api/posts/", {"body": body, "targets": targets, **extra}, format="json")
    assert r.status_code == 201, r.data
    return r.data


def schedule(api, post_id, **payload):
    return api.post(f"/api/posts/{post_id}/schedule/", payload or {"mode": "now"}, format="json")


def text_on(fake_provider, channel):
    return [c.text for ch, c in fake_provider.published if ch == channel.pk]


def test_same_post_follower_gets_the_url(api, blog, social, fake_provider):
    post = create(
        api,
        "The long article",
        [{"channel": blog.id}, {"channel": social.id, "body": "New: {title} {url}", "follows_channel": blog.id}],
        title="Hello",
    )
    follower = next(t for t in post["targets"] if t["channel"] == social.id)
    assert follower["follows_detail"]["channel_name"] == "Blog"
    r = schedule(api, post["id"])
    assert r.status_code == 200, r.data
    blog_url = PostTarget.objects.get(channel=blog).external_url
    assert text_on(fake_provider, social) == [f"New: Hello {blog_url}"]
    assert Post.objects.get(pk=post["id"]).status == "published"


def test_follower_in_another_post_waits_then_appends_the_url(api, blog, social, fake_provider):
    article = create(api, "Article", [{"channel": blog.id}], title="A")
    source_id = article["targets"][0]["id"]
    announce = create(api, "Read my new article", [{"channel": social.id, "follows": source_id}])
    at = timezone.now() + timedelta(hours=2)
    assert schedule(api, article["id"], mode="exact", at=at.isoformat()).status_code == 200
    r = schedule(api, announce["id"], mode="now")
    t = r.data["targets"][0]
    assert t["status"] == "scheduled" and t["scheduled_at"] is None and t["waiting"]
    assert t["schedule_reason"] == "Waits for Blog: right after it is published"
    assert text_on(fake_provider, social) == []
    PostTarget.objects.filter(pk=source_id).update(scheduled_at=timezone.now() - timedelta(seconds=1))
    dispatch_due_targets()
    url = PostTarget.objects.get(pk=source_id).external_url
    assert text_on(fake_provider, social) == [f"Read my new article\n\n{url}"]


def test_untitled_source_lends_its_first_line(api, blog, social, fake_provider):
    post = create(
        api,
        "",
        [
            {"channel": blog.id, "body": "\nBig news today\nMore below"},
            {"channel": social.id, "body": "{title}: {url}", "follows_channel": blog.id},
        ],
    )
    schedule(api, post["id"])
    url = PostTarget.objects.get(channel=blog).external_url
    assert text_on(fake_provider, social) == [f"Big news today: {url}"]


def test_delay_mode(api, blog, social, fake_provider):
    post = create(
        api, "x", [{"channel": blog.id}, {"channel": social.id, "follows_channel": blog.id, "follow_delay": 90}]
    )
    schedule(api, post["id"])
    src, follower = PostTarget.objects.get(channel=blog), PostTarget.objects.get(channel=social)
    assert follower.status == TargetStatus.SCHEDULED
    assert abs((follower.scheduled_at - src.published_at) - timedelta(minutes=90)) < timedelta(seconds=5)
    assert follower.schedule_reason == "After Blog was published"


def test_failed_source_holds_followers_until_retried(api, blog, social, fake_provider):
    post = create(api, "x", [{"channel": blog.id}, {"channel": social.id, "follows_channel": blog.id}])
    fake_provider.fail_with = ProviderError("nope")
    schedule(api, post["id"])
    follower = PostTarget.objects.get(channel=social)
    assert PostTarget.objects.get(channel=blog).status == TargetStatus.FAILED
    assert follower.waiting and "which failed" in follower.last_error
    fake_provider.fail_with = None
    api.post(f"/api/posts/{post['id']}/retry/")
    assert PostTarget.objects.get(channel=social).status == TargetStatus.PUBLISHED


def test_cancelled_source_fails_followers(api, blog, social):
    article = create(api, "a", [{"channel": blog.id}])
    source_id = article["targets"][0]["id"]
    announce = create(api, "b", [{"channel": social.id, "follows": source_id}])
    schedule(api, announce["id"])
    assert api.post(f"/api/targets/{source_id}/cancel/").status_code == 200
    follower = PostTarget.objects.get(channel=social)
    assert follower.status == TargetStatus.FAILED and "cancelled" in follower.last_error


def test_deleted_source_fails_followers(api, blog, social):
    article = create(api, "a", [{"channel": blog.id}])
    announce = create(api, "b", [{"channel": social.id, "follows": article["targets"][0]["id"]}])
    schedule(api, announce["id"])
    api.delete(f"/api/posts/{article['id']}/")
    dispatch_due_targets()
    follower = PostTarget.objects.get(channel=social)
    assert follower.status == TargetStatus.FAILED and "deleted" in follower.last_error


def test_validation_counts_a_realistic_url(api, blog, social):
    post = create(
        api, "x", [{"channel": blog.id}, {"channel": social.id, "body": "y" * 250, "follows_channel": blog.id}]
    )
    r = schedule(api, post["id"])
    assert r.status_code == 400 and "280" in str(r.data)


def test_cycles_other_workspaces_and_unknown_channels_are_refused(api, blog, social, workspace, fake_provider):
    r = api.post(
        "/api/posts/",
        {
            "body": "x",
            "targets": [
                {"channel": blog.id, "follows_channel": social.id},
                {"channel": social.id, "follows_channel": blog.id},
            ],
        },
        format="json",
    )
    assert r.status_code == 400 and "waits for it" in str(r.data)
    assert not Post.objects.exists()
    other = Workspace.objects.create(organization=Organization.objects.create(name="Other"), name="Other")
    elsewhere = Post.objects.create(workspace=other, body="z")
    foreign = PostTarget.objects.create(post=elsewhere, channel=blog)
    r = api.post(
        "/api/posts/", {"body": "x", "targets": [{"channel": social.id, "follows": foreign.id}]}, format="json"
    )
    assert r.status_code == 400 and "Unknown publication" in str(r.data)
    r = api.post(
        "/api/posts/", {"body": "x", "targets": [{"channel": social.id, "follows_channel": blog.id}]}, format="json"
    )
    assert r.status_code == 400 and "not part of this post" in str(r.data)


def test_unfollowing_a_waiting_target_returns_it_to_draft(api, blog, social):
    article = create(api, "a", [{"channel": blog.id}])
    announce = create(api, "b", [{"channel": social.id, "follows": article["targets"][0]["id"]}])
    schedule(api, announce["id"])
    target_id = announce["targets"][0]["id"]
    r = api.patch(f"/api/targets/{target_id}/", {"follows": None}, format="json")
    assert r.status_code == 200, r.data
    assert r.data["status"] == "draft" and r.data["follows"] is None


def test_waiting_target_cannot_be_moved_on_the_calendar(api, blog, social):
    article = create(api, "a", [{"channel": blog.id}])
    announce = create(api, "b", [{"channel": social.id, "follows": article["targets"][0]["id"]}])
    schedule(api, announce["id"])
    r = api.post(
        f"/api/targets/{announce['targets'][0]['id']}/reschedule/",
        {"scheduled_at": (timezone.now() + timedelta(hours=1)).isoformat()},
        format="json",
    )
    assert r.status_code == 400 and "waits for Blog" in str(r.data)


def test_duplicate_keeps_follow_links(api, blog, social):
    post = create(api, "x", [{"channel": blog.id}, {"channel": social.id, "follows_channel": blog.id}])
    copy = api.post(f"/api/posts/{post['id']}/duplicate/").data
    targets = {t["channel"]: t for t in copy["targets"]}
    assert targets[social.id]["follows"] == targets[blog.id]["id"]

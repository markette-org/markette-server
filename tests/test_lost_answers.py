"""A network that publishes the post but whose answer never arrives must not get it twice."""

import json
from datetime import timedelta

import httpx
import pytest
import respx
from django.utils import timezone

from apps.channels.models import Channel
from apps.channels.providers import x as xmod
from apps.channels.providers.bluesky import DEFAULT_SERVICE, BlueskyProvider
from apps.channels.providers.devto import DEFAULT_BASE
from apps.publishing.models import Post, PostTarget, TargetStatus
from apps.publishing.tasks import dispatch_due_targets, requeue_stuck_targets

pytestmark = pytest.mark.django_db(transaction=True)

FAR = timezone.now() + timedelta(days=30)


def lose_answer_once(created, make):
    """A respx side effect: the network creates the post, then the connection drops before it answers."""

    def handler(request):
        created.append(make(request))
        if len(created) == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(201, json=created[-1])

    return handler


@pytest.fixture
def devto(workspace):
    return Channel.create_for(
        workspace=workspace,
        provider="devto",
        external_id="user:7",
        name="DEV",
        credentials={"api_key": "k"},
        config={"default_status": "publish", "username": "ada"},
    )


@pytest.fixture
def x(workspace):
    return Channel.create_for(
        workspace=workspace,
        provider="x",
        external_id="42",
        name="X",
        handle="@ada",
        credentials={"access_token": "t", "refresh_token": "r"},
        token_expires_at=FAR,
    )


@pytest.fixture
def bluesky(workspace, monkeypatch):
    monkeypatch.setattr(BlueskyProvider, "refresh_credentials", lambda self: False)  # the session is fresh
    return Channel.create_for(
        workspace=workspace,
        provider="bluesky",
        external_id="did:plc:ada",
        name="Bluesky",
        handle="@ada.bsky.social",
        credentials={"identifier": "ada", "app_password": "p", "access_jwt": "jwt", "refresh_jwt": "r"},
    )


def publish(api, targets, **extra):
    r = api.post("/api/posts/", {"body": "Body", "title": "Hello", "targets": targets, **extra}, format="json")
    assert r.status_code == 201, r.data
    assert api.post(f"/api/posts/{r.data['id']}/schedule/", {"mode": "now"}, format="json").status_code == 200
    return r.data["id"]


def run_retry(target):
    PostTarget.objects.filter(pk=target.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
    dispatch_due_targets()
    target.refresh_from_db()
    return target


@respx.mock
def test_devto_finds_the_article_instead_of_posting_it_again(api, devto):
    articles = []
    create = respx.post(f"{DEFAULT_BASE}/api/articles").mock(
        side_effect=lose_answer_once(
            articles,
            lambda req: {
                "id": 99,
                "url": "https://dev.to/ada/hello-1abc",
                **json.loads(req.content)["article"],
            },
        )
    )
    respx.get(f"{DEFAULT_BASE}/api/articles/me/all").mock(side_effect=lambda req: httpx.Response(200, json=articles))
    publish(api, [{"channel": devto.id}])
    target = PostTarget.objects.get(channel=devto)
    # DEV can be searched, so the lost answer is retried, not escalated.
    assert target.status == TargetStatus.SCHEDULED and target.extra["maybe_live_since"]
    target = run_retry(target)
    assert target.status == TargetStatus.PUBLISHED
    assert (target.external_id, target.external_url) == ("99", "https://dev.to/ada/hello-1abc")
    assert create.call_count == 1 and "maybe_live_since" not in target.extra


@respx.mock
def test_devto_posts_again_when_the_first_attempt_did_not_land(api, devto):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("read timed out", request=request)  # lost on the way in
        return httpx.Response(201, json={"id": 100, "url": "https://dev.to/ada/hello-2"})

    respx.post(f"{DEFAULT_BASE}/api/articles").mock(side_effect=handler)
    respx.get(f"{DEFAULT_BASE}/api/articles/me/all").mock(return_value=httpx.Response(200, json=[]))
    publish(api, [{"channel": devto.id}])
    target = run_retry(PostTarget.objects.get(channel=devto))
    assert target.status == TargetStatus.PUBLISHED and target.external_id == "100" and len(calls) == 2


@respx.mock
def test_bluesky_finds_the_post_in_its_repository(api, bluesky):
    posts = []
    create = respx.post(f"{DEFAULT_SERVICE}/xrpc/com.atproto.repo.createRecord").mock(
        side_effect=lose_answer_once(
            posts,
            lambda req: {
                "uri": "at://did:plc:ada/app.bsky.feed.post/3kabc",
                "cid": "bafy",
                "value": json.loads(req.content)["record"],
            },
        )
    )
    respx.get(f"{DEFAULT_SERVICE}/xrpc/com.atproto.repo.listRecords").mock(
        side_effect=lambda req: httpx.Response(200, json={"records": posts})
    )
    publish(api, [{"channel": bluesky.id}])
    target = run_retry(PostTarget.objects.get(channel=bluesky))
    assert target.status == TargetStatus.PUBLISHED and create.call_count == 1
    assert target.external_url == "https://bsky.app/profile/ada.bsky.social/post/3kabc"


@respx.mock
def test_x_cannot_tell_so_a_person_checks_and_the_rest_of_the_post_is_untouched(api, devto, x, bluesky):
    respx.post(f"{DEFAULT_BASE}/api/articles").mock(
        return_value=httpx.Response(201, json={"id": 99, "url": "https://dev.to/ada/hello-1abc"})
    )
    tweets = []
    create = respx.post(f"{xmod.API}/tweets").mock(
        side_effect=lose_answer_once(tweets, lambda req: {"data": {"id": "1700"}})
    )
    bsky = respx.post(f"{DEFAULT_SERVICE}/xrpc/com.atproto.repo.createRecord").mock(
        return_value=httpx.Response(200, json={"uri": "at://did:plc:ada/app.bsky.feed.post/3kdef", "cid": "c"})
    )
    post_id = publish(
        api,
        [
            {"channel": devto.id},
            {"channel": x.id, "body": "New: {url}", "follows_channel": devto.id},
            {"channel": bluesky.id, "body": "Also: {url}", "follows_channel": x.id},
        ],
    )
    dev, tweet, sky = (PostTarget.objects.get(channel=c) for c in (devto, x, bluesky))
    assert dev.status == TargetStatus.PUBLISHED
    assert tweet.status == TargetStatus.UNCONFIRMED and "check X" in tweet.last_error
    assert sky.waiting and "needs checking" in sky.last_error
    assert Post.objects.get(pk=post_id).status == "unconfirmed"

    # Nothing republishes it on its own: not the dispatcher, the stuck sweep or "Retry failed".
    dispatch_due_targets()
    requeue_stuck_targets()
    api.post(f"/api/posts/{post_id}/retry/")
    assert create.call_count == 1 and PostTarget.objects.get(channel=x).status == TargetStatus.UNCONFIRMED
    dev_before = PostTarget.objects.get(channel=devto)
    assert (dev_before.status, dev_before.external_id, dev_before.attempts) == (TargetStatus.PUBLISHED, "99", 1)

    # A waiting publication needs the address of the post the person found.
    r = api.post(f"/api/targets/{tweet.id}/resolve/", {"published": True}, format="json")
    assert r.status_code == 400 and "address" in str(r.data)
    r = api.post(
        f"/api/targets/{tweet.id}/resolve/",
        {"published": True, "url": "https://x.com/ada/status/1700"},
        format="json",
    )
    assert r.status_code == 200 and r.data["status"] == "published"
    sky = PostTarget.objects.get(channel=bluesky)
    assert sky.status == TargetStatus.PUBLISHED and bsky.call_count == 1
    assert json.loads(bsky.calls.last.request.content)["record"]["text"] == "Also: https://x.com/ada/status/1700"
    assert create.call_count == 1 and Post.objects.get(pk=post_id).status == "published"


@respx.mock
def test_unconfirmed_post_that_did_not_land_is_published_again(api, x):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("server disconnected", request=request)
        return httpx.Response(201, json={"data": {"id": "1701"}})

    respx.post(f"{xmod.API}/tweets").mock(side_effect=handler)
    publish(api, [{"channel": x.id}])
    tweet = PostTarget.objects.get(channel=x)
    assert tweet.status == TargetStatus.UNCONFIRMED
    assert api.patch(f"/api/targets/{tweet.id}/", {"body": "edited"}, format="json").status_code == 400
    r = api.post(f"/api/targets/{tweet.id}/resolve/", {"published": False}, format="json")
    assert r.status_code == 200
    tweet.refresh_from_db()
    assert tweet.status == TargetStatus.PUBLISHED and tweet.external_id == "1701" and len(calls) == 2


@respx.mock
def test_requests_that_never_left_are_retried_and_gateway_timeouts_are_uncertain(api, x):
    route = respx.post(f"{xmod.API}/tweets")
    route.mock(side_effect=httpx.ConnectError("refused"))
    publish(api, [{"channel": x.id}])
    tweet = PostTarget.objects.get(channel=x)
    assert tweet.status == TargetStatus.SCHEDULED and tweet.next_attempt_at  # plain retry
    route.mock(return_value=httpx.Response(504, text="Gateway Timeout"))
    tweet = run_retry(tweet)
    assert tweet.status == TargetStatus.UNCONFIRMED


@respx.mock
def test_interrupted_worker(workspace, x, devto):
    post = Post.objects.create(workspace=workspace, body="b", title="T")
    old = timezone.now() - timedelta(hours=1)
    for ch in (x, devto):
        PostTarget.objects.create(post=post, channel=ch, status=TargetStatus.PUBLISHING, scheduled_at=old)
    PostTarget.objects.update(updated_at=old)
    requeue_stuck_targets()
    assert PostTarget.objects.get(channel=x).status == TargetStatus.UNCONFIRMED
    dev = PostTarget.objects.get(channel=devto)
    # DEV can be searched: retried, looking for the article first.
    assert dev.status == TargetStatus.SCHEDULED and dev.extra["maybe_live_since"]


@respx.mock
def test_mastodon_retries_with_the_same_idempotency_key(api, workspace):
    masto = Channel.create_for(
        workspace=workspace,
        provider="mastodon",
        external_id="1",
        name="Mastodon",
        base_url="https://social.example",
        credentials={"access_token": "t"},
    )
    statuses = []
    route = respx.post("https://social.example/api/v1/statuses").mock(
        side_effect=lose_answer_once(statuses, lambda req: {"id": "5", "url": "https://social.example/@ada/5"})
    )
    publish(api, [{"channel": masto.id}])
    target = run_retry(PostTarget.objects.get(channel=masto))
    # The server drops the repeat because of the key; Markette only has to send the same one.
    keys = {c.request.headers["idempotency-key"] for c in route.calls}
    assert (
        target.status == TargetStatus.PUBLISHED and route.call_count == 2 and keys == {f"markette-target-{target.pk}"}
    )


@respx.mock
def test_lost_answers_before_the_post_is_sent_are_plain_retries(api, x):
    Channel.objects.filter(pk=x.pk).update(token_expires_at=timezone.now())  # needs a fresh token first
    respx.post(xmod.TOKEN).mock(side_effect=httpx.ReadTimeout("read timed out"))
    tweets = respx.post(f"{xmod.API}/tweets")
    publish(api, [{"channel": x.id}])
    tweet = PostTarget.objects.get(channel=x)
    assert tweet.status == TargetStatus.SCHEDULED and not tweets.called and "maybe_live_since" not in tweet.extra

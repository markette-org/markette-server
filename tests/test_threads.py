import json

import httpx
import pytest
import respx

from apps.channels.models import Channel
from apps.channels.providers import Content
from apps.channels.providers import x as xmod
from apps.channels.providers.bluesky import DEFAULT_SERVICE, BlueskyProvider
from apps.channels.providers.text import measure, split_thread
from apps.publishing.models import PostTarget, TargetStatus

pytestmark = pytest.mark.django_db(transaction=True)

LONG = "\n\n".join(f"Paragraph {i}. " + "word " * 40 for i in range(1, 6)).strip()


def test_split_respects_limits_numbering_and_separators():
    parts = split_thread(LONG, 280, "x")
    assert len(parts) > 1 and all(measure(p, "x") <= 280 for p in parts)
    assert parts[0].endswith(f"1/{len(parts)}") and parts[-1].endswith(f"{len(parts)}/{len(parts)}")
    assert split_thread("short", 280) == ["short"]
    manual = split_thread("First\n---\nSecond\n\n---\nThird", 280, numbering=False)
    assert manual == ["First", "Second", "Third"]
    one_sentence = split_thread("A sentence. " * 60, 100, numbering=False)
    assert all(len(p) <= 100 for p in one_sentence) and all(p.endswith(".") for p in one_sentence)


@pytest.fixture
def x(workspace):
    from django.utils import timezone

    return Channel.create_for(
        workspace=workspace,
        provider="x",
        external_id="42",
        name="X",
        handle="@ada",
        credentials={"access_token": "t", "refresh_token": "r"},
        token_expires_at=timezone.now() + timezone.timedelta(days=30),
    )


def publish(api, channel, body, options):
    post = api.post(
        "/api/posts/", {"body": body, "targets": [{"channel": channel.id, "options": options}]}, format="json"
    ).data
    return api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")


@respx.mock
def test_x_thread_chains_replies(api, x):
    ids = iter(range(100, 200))
    route = respx.post(f"{xmod.API}/tweets").mock(
        side_effect=lambda req: httpx.Response(201, json={"data": {"id": str(next(ids))}})
    )
    # Too long without the option: refused.
    assert publish(api, x, LONG, {}).status_code == 400
    assert publish(api, x, LONG, {"thread": True}).status_code == 200
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert len(bodies) > 1 and "reply" not in bodies[0]
    for prev, body in zip(range(100, 200), bodies[1:], strict=False):
        assert body["reply"] == {"in_reply_to_tweet_id": str(prev)}
    t = PostTarget.objects.get(status=TargetStatus.PUBLISHED)
    assert t.external_id == "100"
    assert [p["id"] for p in t.extra["thread"]["posts"]] == [str(i) for i in range(100, 100 + len(bodies))]


@respx.mock
def test_a_broken_thread_can_be_continued(api, x):
    calls = []

    def answer(req):
        calls.append(req)
        if len(calls) == 2:
            return httpx.Response(503, json={"title": "Service Unavailable"})
        return httpx.Response(201, json={"data": {"id": str(len(calls))}})

    respx.post(f"{xmod.API}/tweets").mock(side_effect=answer)
    publish(api, x, "One\n---\nTwo\n---\nThree", {"thread": True})
    t = PostTarget.objects.get()
    assert t.status == TargetStatus.PUBLISHED and "thread stopped after part 1 of 3" in t.last_error
    r = api.post(f"/api/targets/{t.id}/continue-thread/")
    assert r.status_code == 200, r.data
    t.refresh_from_db()
    assert len(t.extra["thread"]["posts"]) == 3 and t.last_error == ""
    assert json.loads(calls[-1].content)["reply"] == {"in_reply_to_tweet_id": "3"}
    assert api.post(f"/api/targets/{t.id}/continue-thread/").status_code == 400


@respx.mock
def test_bluesky_replies_carry_root_and_parent(workspace, monkeypatch):
    monkeypatch.setattr(BlueskyProvider, "refresh_credentials", lambda self: False)
    ch = Channel.create_for(
        workspace=workspace,
        provider="bluesky",
        external_id="did:plc:ada",
        name="B",
        credentials={"access_jwt": "j", "refresh_jwt": "r", "identifier": "a", "app_password": "p"},
    )
    n = iter(range(1, 10))
    route = respx.post(f"{DEFAULT_SERVICE}/xrpc/com.atproto.repo.createRecord").mock(
        side_effect=lambda req: (
            lambda i: httpx.Response(200, json={"uri": f"at://did:plc:ada/app.bsky.feed.post/{i}", "cid": f"c{i}"})
        )(next(n))
    )
    provider = BlueskyProvider(ch)
    parts = provider.thread_parts(Content(text="A\n---\nB\n---\nC", options={"thread": True}))
    root = provider.publish(parts[0])
    second = provider.publish_reply(parts[1], parent=root, root=root)
    provider.publish_reply(parts[2], parent=second, root=root)
    reply = json.loads(route.calls.last.request.content)["record"]["reply"]
    assert reply == {
        "root": {"uri": "at://did:plc:ada/app.bsky.feed.post/1", "cid": "c1"},
        "parent": {"uri": "at://did:plc:ada/app.bsky.feed.post/2", "cid": "c2"},
    }
    assert json.loads(route.calls.last.request.content)["record"]["text"] == "C\n\n3/3"

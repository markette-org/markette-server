import json
from datetime import timedelta

import httpx
import pytest
import respx
from django.utils import timezone

from apps.channels.models import Channel
from apps.channels.providers import InboxEntry
from apps.channels.providers import x as xmod
from apps.channels.providers.bluesky import DEFAULT_SERVICE, BlueskyProvider
from apps.channels.providers.devto import DEFAULT_BASE
from apps.channels.providers.meta import FacebookProvider, InstagramProvider, graph
from apps.channels.providers.reddit import API as REDDIT
from apps.channels.providers.reddit import RedditProvider
from apps.channels.providers.youtube import API as YT
from apps.channels.providers.youtube import YouTubeProvider
from apps.inbox import tasks
from apps.inbox.models import InboxItem, InboxState
from apps.publishing.models import Post, PostTarget, TargetStatus

pytestmark = pytest.mark.django_db(transaction=True)

NOW = timezone.now()
FAR = NOW + timedelta(days=30)


def ours(workspace, channel, external_id):
    post = Post.objects.create(workspace=workspace, body="Our launch post")
    return PostTarget.objects.create(
        post=post, channel=channel, status=TargetStatus.PUBLISHED, published_at=NOW, external_id=external_id
    )


@pytest.fixture
def masto(workspace):
    return Channel.create_for(
        workspace=workspace,
        provider="mastodon",
        external_id="1",
        name="Mastodon",
        base_url="https://social.example",
        credentials={"access_token": "t"},
    )


def mention(nid, status_id, reply_to=None, ago=timedelta(minutes=5)):
    return {
        "id": nid,
        "type": "mention",
        "status": {
            "id": status_id,
            "content": "<p>@ada great post!<br>Thanks</p>",
            "url": f"https://social.example/@bob/{status_id}",
            "in_reply_to_id": reply_to,
            "visibility": "public",
            "created_at": (NOW - ago).isoformat(),
            "account": {"acct": "bob@other.example", "display_name": "Bob", "avatar": "https://a/b.png", "url": "u"},
        },
    }


@respx.mock
def test_poll_links_to_the_publication_and_replies(api, workspace, masto):
    target = ours(workspace, masto, "500")
    notes = respx.get("https://social.example/api/v1/notifications").mock(
        return_value=httpx.Response(
            200, json=[mention("11", "601", reply_to="500"), mention("10", "600", ago=timedelta(days=5))]
        )
    )
    hook = "https://hooks.example/n"
    api.post(
        "/api/notifications/",
        {"kind": "webhook", "name": "h", "config": {"url": hook}, "events": ["inbox.new"]},
        format="json",
    )
    sent = respx.post(hook).mock(return_value=httpx.Response(200))
    assert tasks.poll_channel(masto.pk) == 1  # the 5-day-old mention arrives already read
    fresh = InboxItem.objects.get(external_id="601")
    assert fresh.text == "@ada great post!\nThanks" and fresh.kind == "reply" and fresh.target == target
    assert InboxItem.objects.get(external_id="600").status == "read"
    assert InboxState.objects.get(channel=masto).state == {"since_id": "11"}
    assert json.loads(sent.calls.last.request.content)["data"]["count"] == 1
    # Next poll asks only for newer notifications and adds nothing twice.
    notes.mock(return_value=httpx.Response(200, json=[]))
    InboxState.objects.update(next_poll_at=None)
    tasks.poll_channel(masto.pk)
    assert notes.calls.last.request.url.params["since_id"] == "11" and InboxItem.objects.count() == 2

    page = api.get("/api/inbox/").data
    assert [i["id"] for i in page["results"]] == [fresh.id, InboxItem.objects.get(external_id="600").id]
    assert page["results"][0]["post"] == target.post_id and page["results"][0]["can_reply"]
    assert api.get("/api/inbox/summary/").data["new"] == 1

    post = respx.post("https://social.example/api/v1/statuses").mock(
        return_value=httpx.Response(200, json={"id": "700", "url": "https://social.example/@ada/700"})
    )
    r = api.post(f"/api/inbox/{fresh.id}/reply/", {"text": "Thank you!"}, format="json")
    assert r.status_code == 200 and r.data["status"] == "done" and r.data["reply_url"].endswith("/700")
    body = json.loads(post.calls.last.request.content)
    assert body == {"status": "@bob@other.example Thank you!", "in_reply_to_id": "601", "visibility": "public"}
    assert api.get("/api/inbox/").data["count"] == 1  # done items leave the open view
    api.post("/api/inbox/mark/", {"ids": [fresh.id], "status": "new"}, format="json")
    assert api.get("/api/inbox/", {"q": "great"}).data["count"] == 2


@respx.mock
def test_missing_permission_is_explained(api, workspace, x):
    respx.get(f"{xmod.API}/users/42/mentions").mock(
        return_value=httpx.Response(403, json={"title": "Forbidden", "detail": "client-not-enrolled"})
    )
    tasks.poll_channel(x.pk)
    row = next(c for c in api.get("/api/inbox/summary/").data["channels"] if c["channel"] == x.pk)
    assert "refused to share replies" in row["last_error"]


@pytest.fixture
def x(workspace):
    return Channel.create_for(
        workspace=workspace,
        provider="x",
        external_id="42",
        name="X",
        credentials={"access_token": "t", "refresh_token": "r"},
        token_expires_at=FAR,
    )


@respx.mock
def test_x_mentions(workspace, x):
    respx.get(f"{xmod.API}/users/42/mentions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "9",
                        "text": "@ada nice",
                        "author_id": "7",
                        "created_at": "2026-10-12T08:00:00Z",
                        "referenced_tweets": [{"type": "replied_to", "id": "5"}],
                    }
                ],
                "includes": {"users": [{"id": "7", "name": "Bob", "username": "bob"}]},
                "meta": {"newest_id": "9"},
            },
        )
    )
    entries, state = x.get_provider().fetch_inbox({}, [])
    assert state == {"since_id": "9"}
    e = entries[0]
    assert (e.kind, e.post_external_id, e.url, e.author_handle) == ("reply", "5", "https://x.com/bob/status/9", "@bob")


@respx.mock
def test_bluesky_notifications_and_reply_threading(workspace, monkeypatch):
    monkeypatch.setattr(BlueskyProvider, "refresh_credentials", lambda self: False)
    ch = Channel(
        organization=workspace.organization,
        provider="bluesky",
        external_id="did:plc:ada",
        credentials={"access_jwt": "j"},
    )
    root = {"uri": "at://did:plc:ada/app.bsky.feed.post/root", "cid": "r"}
    respx.get(f"{DEFAULT_SERVICE}/xrpc/app.bsky.notification.listNotifications").mock(
        return_value=httpx.Response(
            200,
            json={
                "notifications": [
                    {
                        "uri": "at://did:plc:bob/app.bsky.feed.post/abc",
                        "cid": "c1",
                        "reason": "reply",
                        "indexedAt": "2026-10-12T08:00:00Z",
                        "author": {"did": "did:plc:bob", "handle": "bob.bsky.social", "displayName": "Bob"},
                        "record": {
                            "text": "Love it",
                            "createdAt": "2026-10-12T08:00:00Z",
                            "reply": {"root": root, "parent": root},
                        },
                    },
                    {"uri": "at://x/like", "reason": "like", "indexedAt": "2026-10-12T08:01:00Z", "record": {}},
                ]
            },
        )
    )
    p = BlueskyProvider(ch)
    entries, state = p.fetch_inbox({}, [])
    assert len(entries) == 1 and state == {"seen_at": "2026-10-12T08:01:00Z"}
    e = entries[0]
    assert e.post_external_id == root["uri"] and e.url == "https://bsky.app/profile/bob.bsky.social/post/abc"
    assert p.fetch_inbox(state, [])[0] == []  # already seen
    create = respx.post(f"{DEFAULT_SERVICE}/xrpc/com.atproto.repo.createRecord").mock(
        return_value=httpx.Response(200, json={"uri": "at://did:plc:ada/app.bsky.feed.post/r2", "cid": "c2"})
    )
    p.reply_inbox(e, "Thanks!")
    reply = json.loads(create.calls.last.request.content)["record"]["reply"]
    assert reply == {"root": root, "parent": {"uri": "at://did:plc:bob/app.bsky.feed.post/abc", "cid": "c1"}}


@respx.mock
def test_facebook_and_instagram_comments(workspace):
    fb = Channel(
        organization=workspace.organization, provider="facebook", external_id="PAGE", credentials={"access_token": "t"}
    )
    respx.get(f"{graph()}/PAGE_1/comments").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "c1",
                        "message": "Hi!",
                        "from": {"id": "U", "name": "Bob"},
                        "created_time": "2026-10-12T08:00:00+0000",
                    },
                    {"id": "c2", "message": "Thanks", "from": {"id": "PAGE", "name": "Us"}},
                ]
            },
        )
    )
    entries, _ = FacebookProvider(fb).fetch_inbox({}, ["PAGE_1"])
    assert [e.external_id for e in entries] == ["c1"] and entries[0].created_at.year == 2026
    reply = respx.post(f"{graph()}/c1/comments").mock(return_value=httpx.Response(200, json={"id": "c3"}))
    FacebookProvider(fb).reply_inbox(entries[0], "Hello")
    assert b"message=Hello" in reply.calls.last.request.content

    ig = Channel(
        organization=workspace.organization,
        provider="instagram",
        external_id="IG",
        handle="@acme",
        credentials={"access_token": "t"},
    )
    respx.get(f"{graph()}/M1/comments").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "k1",
                        "text": "Wow",
                        "username": "bob",
                        "timestamp": "2026-10-12T08:00:00+0000",
                        "replies": {
                            "data": [
                                {"id": "k2", "text": "thx", "username": "acme"},
                                {"id": "k3", "text": "me too", "username": "eve"},
                            ]
                        },
                    }
                ]
            },
        )
    )
    entries, _ = InstagramProvider(ig).fetch_inbox({}, ["M1"])
    assert [e.external_id for e in entries] == ["k1", "k3"]
    replies = respx.post(f"{graph()}/k1/replies").mock(return_value=httpx.Response(200, json={"id": "k4"}))
    InstagramProvider(ig).reply_inbox(entries[1], "Thanks Eve")  # nested: answered under the top comment
    assert replies.called


@respx.mock
def test_reddit_youtube_wordpress_and_dev(workspace):
    rd = Channel(
        organization=workspace.organization,
        provider="reddit",
        external_id="r/x",
        credentials={"access_token": "t"},
        config={"username": "ada"},
        token_expires_at=FAR,
    )
    respx.get(f"{REDDIT}/comments/abc").mock(
        return_value=httpx.Response(
            200,
            json=[
                {},
                {
                    "data": {
                        "children": [
                            {
                                "kind": "t1",
                                "data": {
                                    "name": "t1_a",
                                    "author": "bob",
                                    "body": "Q?",
                                    "permalink": "/r/x/a",
                                    "created_utc": 1760000000,
                                    "replies": {
                                        "data": {
                                            "children": [
                                                {
                                                    "kind": "t1",
                                                    "data": {
                                                        "name": "t1_b",
                                                        "author": "ada",
                                                        "body": "A",
                                                        "replies": "",
                                                    },
                                                },
                                                {
                                                    "kind": "t1",
                                                    "data": {
                                                        "name": "t1_c",
                                                        "author": "eve",
                                                        "body": "+1",
                                                        "replies": "",
                                                    },
                                                },
                                            ]
                                        }
                                    },
                                },
                            },
                            {"kind": "more", "data": {}},
                        ]
                    }
                },
            ],
        )
    )
    entries, _ = RedditProvider(rd).fetch_inbox({}, ["t3_abc"])
    assert sorted(e.external_id for e in entries) == ["t1_a", "t1_c"]

    yt = Channel(
        organization=workspace.organization,
        provider="youtube",
        external_id="UCme",
        credentials={"access_token": "t", "refresh_token": "r"},
        token_expires_at=FAR,
    )
    respx.get(f"{YT}/commentThreads").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "snippet": {
                            "topLevelComment": {
                                "id": "yc1",
                                "snippet": {
                                    "textOriginal": "Great video",
                                    "authorDisplayName": "Bob",
                                    "authorChannelId": {"value": "UCbob"},
                                    "publishedAt": "2026-10-12T08:00:00Z",
                                },
                            }
                        }
                    }
                ]
            },
        )
    )
    entries, _ = YouTubeProvider(yt).fetch_inbox({}, ["vid1"])
    assert entries[0].url == "https://www.youtube.com/watch?v=vid1&lc=yc1"
    insert = respx.post(f"{YT}/comments").mock(return_value=httpx.Response(200, json={"id": "yc2"}))
    YouTubeProvider(yt).reply_inbox(entries[0], "Thanks")
    assert json.loads(insert.calls.last.request.content) == {"snippet": {"parentId": "yc1", "textOriginal": "Thanks"}}

    site = "https://blog.example"
    wp = Channel(
        organization=workspace.organization,
        provider="wordpress",
        base_url=site,
        credentials={"username": "ada", "app_password": "p"},
    )
    respx.get(f"{site}/wp-json/wp/v2/comments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 3,
                    "post": 12,
                    "author_name": "Bob",
                    "content": {"rendered": "<p>Nice &amp; clear</p>"},
                    "date_gmt": "2026-10-12T08:00:00",
                    "link": f"{site}/p#comment-3",
                }
            ],
        )
    )
    entries, _ = wp.get_provider().fetch_inbox({}, ["12"])
    assert entries[0].text == "Nice & clear" and entries[0].post_external_id == "12"
    post = respx.post(f"{site}/wp-json/wp/v2/comments").mock(return_value=httpx.Response(201, json={"id": 4}))
    wp.get_provider().reply_inbox(entries[0], "Thanks")
    assert json.loads(post.calls.last.request.content) == {"post": 12, "parent": 3, "content": "Thanks"}

    dev = Channel(
        organization=workspace.organization, provider="devto", credentials={"api_key": "k"}, config={"username": "ada"}
    )
    respx.get(f"{DEFAULT_BASE}/api/comments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id_code": "1a",
                    "body_html": "<p>Cool</p>",
                    "user": {"username": "bob", "name": "Bob"},
                    "children": [
                        {"id_code": "1b", "body_html": "<p>Thanks</p>", "user": {"username": "ada"}, "children": []}
                    ],
                }
            ],
        )
    )
    entries, _ = dev.get_provider().fetch_inbox({}, ["99"])
    assert [e.external_id for e in entries] == ["1a"] and entries[0].url == "https://dev.to/bob/comment/1a"
    assert not dev.get_provider().capabilities.inbox_reply


def test_networks_without_replies_say_so(api, workspace):
    dev = Channel.create_for(
        workspace=workspace, provider="devto", external_id="u", name="DEV", credentials={"api_key": "k"}
    )
    item = InboxItem.objects.create(channel=dev, external_id="1a", kind="comment", text="hi")
    r = api.post(f"/api/inbox/{item.id}/reply/", {"text": "x"}, format="json")
    assert r.status_code == 400 and "answer on the network" in str(r.data)
    assert InboxEntry  # imported for providers' contract


@respx.mock
def test_threads_and_linkedin_pages(workspace):
    from apps.channels.providers.linkedin import REST, LinkedInProvider
    from apps.channels.providers.threads import API as THREADS
    from apps.channels.providers.threads import ThreadsProvider

    th = Channel(
        organization=workspace.organization,
        provider="threads",
        external_id="me",
        handle="@acme",
        credentials={"access_token": "t"},
    )
    respx.get(f"{THREADS}/T1/replies").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "r1",
                        "text": "Yes!",
                        "username": "bob",
                        "timestamp": "2026-10-12T08:00:00+0000",
                        "permalink": "https://www.threads.com/@bob/post/r1",
                    },
                    {"id": "r2", "text": "thx", "username": "acme"},
                ]
            },
        )
    )
    entries, _ = ThreadsProvider(th).fetch_inbox({}, ["T1"])
    assert [e.external_id for e in entries] == ["r1"]

    page = Channel(
        organization=workspace.organization,
        provider="linkedin",
        external_id="urn:li:organization:5",
        credentials={"access_token": "t", "author": "urn:li:organization:5"},
        token_expires_at=FAR,
    )
    person = Channel(
        organization=workspace.organization,
        provider="linkedin",
        external_id="urn:li:person:a",
        credentials={"access_token": "t", "author": "urn:li:person:a"},
        token_expires_at=FAR,
    )
    respx.get(url__startswith=f"{REST}/socialActions/").mock(
        return_value=httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "commentUrn": "urn:li:comment:(urn:li:share:1,9)",
                        "actor": "urn:li:person:b",
                        "message": {"text": "Congrats"},
                        "created": {"time": 1760000000000},
                    }
                ]
            },
        )
    )
    entries, _ = LinkedInProvider(page).fetch_inbox({}, ["urn:li:share:1"])
    assert entries[0].text == "Congrats" and entries[0].context == {"object": "urn:li:share:1"}
    assert LinkedInProvider(person).fetch_inbox({}, ["urn:li:share:1"]) == ([], {})

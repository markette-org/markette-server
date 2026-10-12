from datetime import timedelta

import httpx
import pytest
import respx
from django.utils import timezone

from apps.channels.models import Channel
from apps.channels.providers.bluesky import APPVIEW, BlueskyProvider
from apps.channels.providers.devto import DEFAULT_BASE
from apps.channels.providers.reddit import API as REDDIT
from apps.channels.providers.reddit import RedditProvider
from apps.channels.providers.youtube import API as YT
from apps.channels.providers.youtube import YouTubeProvider
from apps.insights.models import BestTimeProfile
from apps.publishing.models import Post, PostTarget, TargetStatus

pytestmark = pytest.mark.django_db(transaction=True)
FAR = timezone.now() + timedelta(days=30)


def status(sid, days_ago, likes=3):
    return {
        "id": sid,
        "content": f"<p>Post {sid}</p>",
        "url": f"https://social.example/@ada/{sid}",
        "created_at": (timezone.now() - timedelta(days=days_ago)).isoformat(),
        "favourites_count": likes,
        "replies_count": 1,
        "reblogs_count": 0,
    }


@respx.mock
def test_mastodon_import_creates_published_posts_once(api, workspace):
    ch = Channel.create_for(
        workspace=workspace,
        provider="mastodon",
        external_id="77",
        name="Mastodon",
        base_url="https://social.example",
        credentials={"access_token": "t"},
    )
    route = respx.get("https://social.example/api/v1/accounts/77/statuses").mock(
        side_effect=[
            httpx.Response(200, json=[status("3", 1), status("2", 3)]),
            httpx.Response(200, json=[status("1", 9)]),
            httpx.Response(200, json=[]),
        ]
    )
    assert api.get(f"/api/channels/{ch.id}/import-history/").data is None
    r = api.post(f"/api/channels/{ch.id}/import-history/", {"limit": 50}, format="json")
    assert r.status_code == 200 and r.data["status"] == "done" and r.data["imported"] == 3
    assert route.calls[1].request.url.params["max_id"] == "2"
    targets = PostTarget.objects.filter(channel=ch).order_by("published_at")
    assert [t.external_id for t in targets] == ["1", "2", "3"]
    t = targets.last()
    assert t.status == TargetStatus.PUBLISHED and t.post.imported and t.post.body == "Post 3"
    assert t.metrics["likes"] == 3 and t.metrics["engagements"] == 4
    assert api.get(f"/api/posts/{t.post_id}/").data["imported"] is True
    assert BestTimeProfile.objects.filter(channel=ch).exists()
    # A second import skips what is already there.
    route.side_effect = [httpx.Response(200, json=[status("4", 0), status("3", 1)]), httpx.Response(200, json=[])]
    r = api.post(f"/api/channels/{ch.id}/import-history/", {}, format="json")
    assert r.data["imported"] == 1 and Post.objects.filter(imported=True).count() == 4


def test_networks_without_history_refuse(api, workspace, fake_provider):
    ch = Channel.create_for(workspace=workspace, provider="fake", external_id="1", name="F")
    assert api.post(f"/api/channels/{ch.id}/import-history/", {}, format="json").status_code == 400


@respx.mock
def test_failures_are_reported(api, workspace):
    ch = Channel.create_for(
        workspace=workspace, provider="devto", external_id="u", name="DEV", credentials={"api_key": "k"}
    )
    respx.get(f"{DEFAULT_BASE}/api/articles/me/published").mock(return_value=httpx.Response(500, json={}))
    r = api.post(f"/api/channels/{ch.id}/import-history/", {}, format="json")
    assert r.data["status"] == "failed" and "500" in r.data["error"]


@respx.mock
def test_provider_listings(workspace, monkeypatch):
    monkeypatch.setattr(BlueskyProvider, "refresh_credentials", lambda self: False)
    bs = Channel(
        organization=workspace.organization,
        provider="bluesky",
        external_id="did:plc:ada",
        handle="@ada.bsky.social",
        credentials={},
    )
    respx.get(f"{APPVIEW}/xrpc/app.bsky.feed.getAuthorFeed").mock(
        return_value=httpx.Response(
            200,
            json={
                "feed": [
                    {
                        "post": {
                            "uri": "at://did:plc:ada/app.bsky.feed.post/a1",
                            "author": {"did": "did:plc:ada"},
                            "record": {"text": "Mine", "createdAt": "2026-10-01T10:00:00Z"},
                            "likeCount": 4,
                        }
                    },
                    {
                        "post": {
                            "uri": "at://did:plc:bob/app.bsky.feed.post/b1",
                            "author": {"did": "did:plc:bob"},
                            "record": {"text": "Reposted"},
                        },
                        "reason": {"$type": "app.bsky.feed.defs#reasonRepost"},
                    },
                ]
            },
        )
    )
    [e] = BlueskyProvider(bs).fetch_history(10)
    assert e.url == "https://bsky.app/profile/ada.bsky.social/post/a1" and e.metrics.likes == 4

    yt = Channel(
        organization=workspace.organization,
        provider="youtube",
        external_id="UCme",
        credentials={"access_token": "t"},
        token_expires_at=FAR,
    )
    respx.get(f"{YT}/channels").mock(
        return_value=httpx.Response(
            200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUme"}}}]}
        )
    )
    respx.get(f"{YT}/playlistItems").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "snippet": {
                            "title": "Launch",
                            "publishedAt": "2026-10-01T10:00:00Z",
                            "resourceId": {"videoId": "v1"},
                        }
                    }
                ]
            },
        )
    )
    [e] = YouTubeProvider(yt).fetch_history(10)
    assert (e.external_id, e.title, e.url) == ("v1", "Launch", "https://www.youtube.com/watch?v=v1")

    rd = Channel(
        organization=workspace.organization,
        provider="reddit",
        external_id="r/acme",
        credentials={"access_token": "t"},
        config={"username": "ada", "subreddit": "acme"},
        token_expires_at=FAR,
    )
    respx.get(f"{REDDIT}/user/ada/submitted").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "after": None,
                    "children": [
                        {
                            "data": {
                                "name": "t3_a",
                                "subreddit": "acme",
                                "title": "Ours",
                                "created_utc": 1760000000,
                                "score": 9,
                                "num_comments": 2,
                                "permalink": "/r/acme/a",
                            }
                        },
                        {
                            "data": {
                                "name": "t3_b",
                                "subreddit": "other",
                                "title": "Elsewhere",
                                "created_utc": 1760000000,
                            }
                        },
                    ],
                }
            },
        )
    )
    [e] = RedditProvider(rd).fetch_history(10)
    assert e.external_id == "t3_a" and e.metrics.likes == 9

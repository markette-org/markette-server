import json
from pathlib import Path

import httpx
import pytest
import respx
from django.utils import timezone

from apps.channels.models import Channel
from apps.channels.providers import Content, MediaItem
from apps.channels.providers.blogs import WordPressProvider
from apps.channels.providers.telegram import TelegramProvider
from apps.publishing.models import Post, PostTarget, TargetStatus

pytestmark = pytest.mark.django_db(transaction=True)


def published(workspace, channel, external_id, **extra):
    post = Post.objects.create(workspace=workspace, body="Old text", title="Old")
    return PostTarget.objects.create(
        post=post,
        channel=channel,
        status=TargetStatus.PUBLISHED,
        published_at=timezone.now(),
        external_id=external_id,
        external_url="https://example.org/p",
        extra=extra,
    )


@respx.mock
def test_mastodon_edit_keeps_its_media(api, workspace):
    ch = Channel.create_for(
        workspace=workspace,
        provider="mastodon",
        external_id="1",
        name="M",
        base_url="https://social.example",
        credentials={"access_token": "t"},
    )
    t = published(workspace, ch, "77")
    respx.get("https://social.example/api/v1/statuses/77").mock(
        return_value=httpx.Response(200, json={"id": "77", "media_attachments": [{"id": "m1"}], "spoiler_text": ""})
    )
    put = respx.put("https://social.example/api/v1/statuses/77").mock(return_value=httpx.Response(200, json={}))
    r = api.post(f"/api/targets/{t.id}/edit-remote/", {"body": "Fixed typo"}, format="json")
    assert r.status_code == 200, r.data
    assert json.loads(put.calls.last.request.content) == {
        "status": "Fixed typo",
        "media_ids": ["m1"],
        "spoiler_text": "",
    }
    t.refresh_from_db()
    assert t.body == "Fixed typo" and t.extra["edited_at"]


@respx.mock
def test_failed_edit_keeps_the_old_text(api, workspace):
    ch = Channel.create_for(
        workspace=workspace,
        provider="mastodon",
        external_id="1",
        name="M",
        base_url="https://social.example",
        credentials={"access_token": "t"},
    )
    t = published(workspace, ch, "77")
    respx.get("https://social.example/api/v1/statuses/77").mock(return_value=httpx.Response(404, json={}))
    r = api.post(f"/api/targets/{t.id}/edit-remote/", {"body": "New"}, format="json")
    assert r.status_code == 400
    t.refresh_from_db()
    assert t.body == ""  # still the shared text


def test_networks_without_edits_refuse(api, workspace):
    ch = Channel.create_for(workspace=workspace, provider="x", external_id="1", name="X", credentials={})
    t = published(workspace, ch, "5")
    r = api.post(f"/api/targets/{t.id}/edit-remote/", {"body": "x"}, format="json")
    assert r.status_code == 400 and "cannot be edited" in str(r.data)


@respx.mock
def test_wordpress_edit_reuses_uploaded_images(workspace, tmp_path):
    site = "https://blog.example"
    ch = Channel(
        organization=workspace.organization,
        provider="wordpress",
        base_url=site,
        credentials={"username": "a", "app_password": "p"},
    )
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    item = MediaItem(path=Path(p), url="https://markette.example/media/a.png", mime="image/png")
    route = respx.post(f"{site}/wp-json/wp/v2/posts/12").mock(
        return_value=httpx.Response(200, json={"id": 12, "link": f"{site}/new-slug"})
    )
    changes = WordPressProvider(ch).update(
        "12",
        Content(text="Hello ![pic](https://markette.example/media/a.png)", title="New", media=[item]),
        {"media_map": {"https://markette.example/media/a.png": f"{site}/wp-content/a.png"}},
    )
    body = json.loads(route.calls.last.request.content)
    assert body["title"] == "New" and f"{site}/wp-content/a.png" in body["content"]
    assert changes == {"url": f"{site}/new-slug"}


@respx.mock
def test_telegram_edits_text_or_caption(workspace):
    ch = Channel(
        organization=workspace.organization, provider="telegram", external_id="-100", credentials={"bot_token": "1:a"}
    )
    text = respx.post("https://api.telegram.org/bot1:a/editMessageText").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    cap = respx.post("https://api.telegram.org/bot1:a/editMessageCaption").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    TelegramProvider(ch).update("-100:5", Content(text="new"), {"edit": "text"})
    TelegramProvider(ch).update("-100:6", Content(text="cap"), {"edit": "caption"})
    assert json.loads(text.calls.last.request.content) == {"chat_id": "-100", "message_id": 5, "text": "new"}
    assert json.loads(cap.calls.last.request.content)["caption"] == "cap"


def test_backlinks_are_added_to_the_source_once_announcements_are_live(api, workspace, fake_provider, monkeypatch):
    edits = []
    monkeypatch.setattr(fake_provider.capabilities, "supports_edit", True)
    monkeypatch.setattr(fake_provider, "update", lambda self, ext, content, extra: edits.append((ext, content.text)))
    blog = Channel.create_for(workspace=workspace, provider="fake", external_id="blog", name="Blog")
    social = Channel.create_for(workspace=workspace, provider="fake", external_id="x", name="Social")
    post = api.post(
        "/api/posts/",
        {
            "body": "The article",
            "targets": [
                {"channel": blog.id, "options": {"backlinks": True}},
                {"channel": social.id, "body": "New: {url}", "follows_channel": blog.id},
            ],
        },
        format="json",
    ).data
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    src = PostTarget.objects.get(channel=blog)
    follower = PostTarget.objects.get(channel=social)
    assert follower.status == TargetStatus.PUBLISHED
    assert edits == [(src.external_id, f"The article\n\nDiscuss this post on Fake: {follower.external_url}")]
    assert src.extra["backlinks"] == [{"label": "Fake", "url": follower.external_url}]

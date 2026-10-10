import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
import respx

from apps.channels.models import Channel
from apps.channels.providers import Content, MediaItem, ProviderError
from apps.channels.providers import threads as th
from apps.channels.providers import youtube as yt
from apps.channels.providers.discord import DiscordProvider, split_text
from apps.channels.providers.newsletters import BREVO, BrevoProvider, GhostProvider, ListmonkProvider
from apps.channels.providers.webhook import WebhookProvider, sign
from apps.platform import conf

pytestmark = pytest.mark.django_db


def media(tmp_path, name="a.jpg", mime="image/jpeg", data=b"img"):
    p = tmp_path / name
    p.write_bytes(data)
    return MediaItem(path=Path(p), url=f"https://m.example/{name}", mime=mime, filename=name, size=len(data))


def ch(workspace, provider, external_id="1", **kw):
    return Channel(organization=workspace.organization, provider=provider, external_id=external_id, name=provider, **kw)


# --- Threads ----------------------------------------------------------------------------


@respx.mock
def test_threads_oauth_exchanges_long_lived_token(settings, db):
    settings.THREADS_APP_ID, settings.THREADS_APP_SECRET = "tid", "tsecret"
    conf.invalidate()
    respx.post("https://graph.threads.net/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "short", "user_id": 7})
    )
    long = respx.get("https://graph.threads.net/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "long", "expires_in": 5184000})
    )
    respx.get(f"{th.API}/me").mock(
        return_value=httpx.Response(
            200, json={"id": "7", "username": "acme", "name": "Acme", "threads_profile_picture_url": "https://p"}
        )
    )
    [c] = th.ThreadsProvider().complete_oauth(SimpleNamespace(), {"code": "c"})
    assert c.credentials == {"access_token": "long"} and c.profile_url == "https://www.threads.com/@acme"
    assert long.calls.last.request.url.params["grant_type"] == "th_exchange_token"
    conf.invalidate()


@respx.mock
def test_threads_carousel_and_reply_comment(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(th.time, "sleep", lambda s: None)
    c = ch(workspace, "threads", "7", credentials={"access_token": "tok"})
    ids = iter(["c1", "c2", "car", "post-id", "r1", "reply-id"])
    create = respx.post(f"{th.API}/7/threads").mock(side_effect=lambda r: httpx.Response(200, json={"id": next(ids)}))
    publish = respx.post(f"{th.API}/7/threads_publish").mock(
        side_effect=lambda r: httpx.Response(200, json={"id": next(ids)})
    )
    respx.get(url__regex=rf"{th.API}/car\?.*").mock(return_value=httpx.Response(200, json={"status": "FINISHED"}))
    respx.get(url__regex=rf"{th.API}/post-id\?.*").mock(
        return_value=httpx.Response(200, json={"permalink": "https://www.threads.com/@acme/post/x"})
    )
    res = th.ThreadsProvider(c).publish(Content(text="Two pics", media=[media(tmp_path), media(tmp_path, "b.jpg")]))
    assert (res.external_id, res.url) == ("post-id", "https://www.threads.com/@acme/post/x")
    bodies = [dict(httpx.QueryParams(call.request.content.decode())) for call in create.calls]
    assert bodies[0]["is_carousel_item"] == "true" and bodies[2]["media_type"] == "CAROUSEL"
    assert bodies[2]["children"] == "c1,c2"
    assert th.ThreadsProvider(c).post_first_comment("post-id", "Link: https://acme.example") == "reply-id"
    reply = dict(httpx.QueryParams(create.calls.last.request.content.decode()))
    assert reply["reply_to_id"] == "post-id" and reply["media_type"] == "TEXT"
    assert publish.call_count == 2
    text = " ".join(f"https://l{i}.example" for i in range(6))
    assert "at most 5 different links" in th.ThreadsProvider(c).validate(Content(text=text))[0]


# --- YouTube -----------------------------------------------------------------------------


@respx.mock
def test_youtube_resumable_upload_and_private_note(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(yt, "CHUNK", 4)
    c = ch(
        workspace,
        "youtube",
        "UC1",
        credentials={"access_token": "tok", "refresh_token": "r"},
        config={"privacy": "public", "category_id": "22"},
    )
    init = respx.post(yt.UPLOAD).mock(return_value=httpx.Response(200, headers={"Location": "https://up.example/s1"}))
    chunks = []

    def put(req):
        chunks.append(req.headers["Content-Range"])
        if req.headers["Content-Range"].endswith("/10") and req.headers["Content-Range"].startswith("bytes 8-"):
            return httpx.Response(200, json={"id": "vid1", "status": {"privacyStatus": "private"}})
        return httpx.Response(308)

    respx.put("https://up.example/s1").mock(side_effect=put)
    video = media(tmp_path, "v.mp4", "video/mp4", b"0123456789")
    res = yt.YouTubeProvider(c).publish(Content(text="Desc", title="My video", media=[video], tags=["a"]))
    assert chunks == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]
    meta = json.loads(init.calls.last.request.content)
    assert meta["snippet"]["title"] == "My video" and meta["status"]["privacyStatus"] == "public"
    assert res.url == "https://www.youtube.com/watch?v=vid1" and "API audit" in res.extra["note"]
    assert yt.YouTubeProvider(c).validate(Content(text="x", title="t", media=[media(tmp_path)])) == [
        "YouTube needs a video."
    ]


# --- Discord -----------------------------------------------------------------------------


def test_discord_split_text():
    parts = split_text("a" * 1500 + "\n" + "b" * 1500)
    assert [len(p) for p in parts] == [1500, 1500]


@respx.mock
def test_discord_publish_with_files_and_long_text(workspace, tmp_path):
    hook = "https://discord.com/api/webhooks/123/tok"
    c = ch(
        workspace, "discord", "555", credentials={"webhook_url": hook}, config={"guild_id": "999", "username": "News"}
    )
    sends = respx.post(hook).mock(
        side_effect=[
            httpx.Response(200, json={"id": "m1", "channel_id": "555"}),
            httpx.Response(200, json={"id": "m2", "channel_id": "555"}),
        ]
    )
    res = DiscordProvider(c).publish(Content(text="x" * 2500, media=[media(tmp_path)]))
    assert res.url == "https://discord.com/channels/999/555/m1" and sends.call_count == 2
    first = sends.calls[0].request
    assert b'name="files[0]"' in first.content and b'"parse": []' in first.content
    assert json.loads(sends.calls[1].request.content)["username"] == "News"


@respx.mock
def test_discord_connect_validates_url():
    with pytest.raises(ProviderError, match="does not look like"):
        DiscordProvider().connect_with_credentials({"webhook_url": "https://evil.example/webhooks/1/2"})
    respx.get("https://discord.com/api/webhooks/1/t").mock(
        return_value=httpx.Response(
            200, json={"id": "1", "name": "Markette", "channel_id": "555", "guild_id": "999", "avatar": None}
        )
    )
    [cand] = DiscordProvider().connect_with_credentials({"webhook_url": "https://discord.com/api/webhooks/1/t"})
    assert cand.external_id == "555" and cand.profile_url == "https://discord.com/channels/999/555"


# --- Brevo / Listmonk / Ghost --------------------------------------------------------------


@respx.mock
def test_brevo_campaign_and_stats(workspace, tmp_path):
    c = ch(
        workspace,
        "brevo",
        "4",
        credentials={"api_key": "k"},
        config={"sender_email": "news@acme.org", "sender_name": "Acme"},
    )
    create = respx.post(f"{BREVO}/emailCampaigns").mock(return_value=httpx.Response(201, json={"id": 77}))
    send = respx.post(f"{BREVO}/emailCampaigns/77/sendNow").mock(return_value=httpx.Response(204))
    res = BrevoProvider(c).publish(
        Content(
            text="Hello **readers**",
            title="October news",
            link="https://acme.org",
            media=[media(tmp_path)],
            idempotency_key="markette-target-5",
            options={"preheader": "What's new"},
        )
    )
    body = json.loads(create.calls.last.request.content)
    assert body["recipients"] == {"listIds": [4]} and body["previewText"] == "What's new"
    assert "{{ unsubscribe }}" in body["htmlContent"] and "<strong>readers</strong>" in body["htmlContent"]
    assert create.calls.last.request.headers["api-key"] == "k" and send.called and res.external_id == "77"
    respx.get(f"{BREVO}/emailCampaigns/77").mock(
        return_value=httpx.Response(
            200, json={"statistics": {"globalStats": {"delivered": 900, "uniqueViews": 400, "uniqueClicks": 55}}}
        )
    )
    m = BrevoProvider(c).fetch_metrics(["77"])["77"]
    assert (m.impressions, m.reach, m.clicks) == (900, 400, 55)


@respx.mock
def test_listmonk_campaign_runs(workspace):
    c = ch(
        workspace,
        "listmonk",
        "3",
        base_url="https://lists.acme.org",
        credentials={"api_user": "markette", "api_token": "t"},
        config={"from_email": "Acme <n@acme.org>"},
    )
    create = respx.post("https://lists.acme.org/api/campaigns").mock(
        return_value=httpx.Response(200, json={"data": {"id": 12}})
    )
    start = respx.put("https://lists.acme.org/api/campaigns/12/status").mock(
        return_value=httpx.Response(200, json={"data": {"id": 12, "status": "running"}})
    )
    ListmonkProvider(c).publish(Content(text="Hi", title="Issue 1"))
    body = json.loads(create.calls.last.request.content)
    assert body["lists"] == [3] and body["content_type"] == "html" and "{{ UnsubscribeURL }}" in body["body"]
    assert create.calls.last.request.headers["authorization"] == "token markette:t"
    assert json.loads(start.calls.last.request.content) == {"status": "running"}


@respx.mock
def test_ghost_signed_key_publish_and_email(workspace, tmp_path):
    key = "6489b7fdfa6b2f001fc8a2d7:" + "ab" * 32
    c = ch(
        workspace,
        "ghost",
        "https://blog.acme.org/",
        base_url="https://blog.acme.org",
        credentials={"admin_key": key},
        config={"newsletter": "default-newsletter"},
    )
    respx.post("https://blog.acme.org/ghost/api/admin/images/upload/").mock(
        return_value=httpx.Response(201, json={"images": [{"url": "https://blog.acme.org/content/images/a.jpg"}]})
    )
    posts = respx.post("https://blog.acme.org/ghost/api/admin/posts/").mock(
        return_value=httpx.Response(201, json={"posts": [{"id": "p1", "url": "https://blog.acme.org/launch/"}]})
    )
    res = GhostProvider(c).publish(Content(text="Body", title="Launch", tags=["news"], media=[media(tmp_path)]))
    req = posts.calls.last.request
    token = req.headers["authorization"].split(" ", 1)[1]
    claims = jwt.decode(token, bytes.fromhex("ab" * 32), algorithms=["HS256"], audience="/admin/")
    assert (
        jwt.get_unverified_header(token)["kid"] == "6489b7fdfa6b2f001fc8a2d7" and claims["exp"] - claims["iat"] == 300
    )
    assert req.url.params["newsletter"] == "default-newsletter" and req.url.params["source"] == "html"
    post = json.loads(req.content)["posts"][0]
    assert post["feature_image"].endswith("/a.jpg") and post["tags"] == [{"name": "news"}]
    assert res.url == "https://blog.acme.org/launch/" and res.extra["emailed"] is True


# --- Webhook -----------------------------------------------------------------------------


@respx.mock
def test_webhook_signed_payload_and_answer(workspace, tmp_path):
    route = respx.post("https://n8n.acme.org/webhook/markette").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    [cand] = WebhookProvider().connect_with_credentials(
        {"url": "https://n8n.acme.org/webhook/markette", "headers": "X-Api-Key: abc"}
    )
    assert json.loads(route.calls.last.request.content)["event"] == "ping"
    c = ch(workspace, "webhook", cand.external_id, credentials=cand.credentials)
    route.mock(return_value=httpx.Response(200, json={"id": "row-9", "url": "https://sheet.example/9"}))
    res = WebhookProvider(c).publish(
        Content(text="Hello", title="T", media=[media(tmp_path)], idempotency_key="markette-target-42")
    )
    req = route.calls.last.request
    expected = sign(cand.credentials["secret"], req.headers["X-Markette-Timestamp"], req.content)
    assert hmac.compare_digest(req.headers["X-Markette-Signature"], expected)
    assert req.headers["X-Api-Key"] == "abc"
    body = json.loads(req.content)
    assert body["event"] == "publication.created" and body["target_id"] == 42
    assert body["post"]["media"][0]["url"] == "https://m.example/a.jpg"
    assert (res.external_id, res.url) == ("row-9", "https://sheet.example/9")
    route.mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderError) as exc:
        WebhookProvider(c).publish(Content(text="x", idempotency_key="markette-target-43"))
    assert exc.value.retryable


def test_webhook_secret_only_for_admins(workspace, user):
    from rest_framework.test import APIClient

    from apps.core.models import User
    from apps.tenants.models import Membership

    Channel.create_for(
        workspace=workspace,
        provider="webhook",
        external_id="n8n/x",
        name="n8n",
        credentials={"url": "https://n8n.example/x", "secret": "s3", "headers": {}},
    )
    viewer = User.objects.create_user("v@example.com", "pw-pw-pw-pw-1")
    Membership.objects.create(workspace=workspace, user=viewer, role="viewer")
    seen = {}
    for who in (user, viewer):
        c = APIClient()
        c.force_authenticate(who)
        c.credentials(HTTP_X_WORKSPACE=workspace.slug)
        seen[who.email] = c.get("/api/channels/").data["results"][0]["setup"]["signing_secret"]
    assert seen == {user.email: "s3", "v@example.com": None}


def test_rotate_webhook_secret(api, workspace):
    c = Channel.create_for(
        workspace=workspace,
        provider="webhook",
        external_id="n8n/y",
        name="n8n",
        credentials={"url": "https://n8n.example/y", "secret": "old", "headers": {}},
    )
    r = api.post(f"/api/channels/{c.id}/rotate-secret/")
    assert r.status_code == 200 and r.data["setup"]["signing_secret"] not in ("old", None)

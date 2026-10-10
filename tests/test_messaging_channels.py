import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.utils import timezone
from rest_framework.test import APIClient

from apps.channels.models import Channel, Delivery, InboundChat, Subscriber
from apps.channels.providers import Content, MediaItem, ProviderError
from apps.channels.providers import qq as qqmod
from apps.channels.providers import wechat as wxmod
from apps.channels.providers.qq import QQProvider
from apps.channels.providers.wechat import WeChatProvider
from apps.channels.providers.whatsapp import WhatsAppProvider, graph
from apps.platform import conf
from apps.publishing.models import PostTarget, TargetStatus

pytestmark = pytest.mark.django_db(transaction=True)


# --- WhatsApp ---------------------------------------------------------------------------


def wa_channel(workspace):
    return Channel.create_for(
        workspace=workspace,
        provider="whatsapp",
        external_id="111",
        name="Acme",
        credentials={"access_token": "tok"},
        config={
            "waba_id": "222",
            "template_name": "news",
            "template_language": "en",
            "template_has_image": True,
            "join_keyword": "JOIN",
            "stop_keyword": "STOP",
            "verify_token": "vt-123",
        },
    )


@respx.mock
def test_whatsapp_connect_checks_template():
    respx.get(f"{graph()}/111").mock(
        return_value=httpx.Response(
            200, json={"display_phone_number": "+39 333 000 1111", "verified_name": "Acme News"}
        )
    )
    tpl = respx.get(f"{graph()}/222/message_templates").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "news",
                        "language": "en",
                        "status": "APPROVED",
                        "components": [{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "📰 {{1}}"}],
                    }
                ]
            },
        )
    )
    [c] = WhatsAppProvider().connect_with_credentials(
        {"phone_number_id": "111", "waba_id": "222", "access_token": "tok", "template_name": "news"}
    )
    assert c.name == "Acme News" and c.config["template_has_image"] is True and len(c.config["verify_token"]) == 32
    assert c.profile_url == "https://wa.me/393330001111"
    tpl.mock(
        return_value=httpx.Response(
            200, json={"data": [{"name": "news", "language": "en", "status": "PENDING", "components": []}]}
        )
    )
    with pytest.raises(ProviderError, match="APPROVED"):
        WhatsAppProvider().connect_with_credentials(
            {"phone_number_id": "111", "waba_id": "222", "access_token": "tok", "template_name": "news"}
        )


@respx.mock
def test_whatsapp_broadcast_to_opted_in_subscribers_and_webhook(api, workspace, settings, tmp_path):
    ch = wa_channel(workspace)
    now = timezone.now()
    Subscriber.objects.create(channel=ch, address="393330000001", opted_in_at=now)
    Subscriber.objects.create(channel=ch, address="393330000002", opted_in_at=now)
    Subscriber.objects.create(channel=ch, address="393330000003", opted_in_at=now, opted_out_at=now)
    counter = iter(range(100))
    sends = respx.post(f"{graph()}/111/messages").mock(
        side_effect=lambda req: httpx.Response(200, json={"messages": [{"id": f"wamid.{next(counter)}"}]})
    )
    post = api.post("/api/posts/", {"body": "Big news today", "targets": [{"channel": ch.id}]}, format="json").data
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    t = PostTarget.objects.get(post_id=post["id"])
    assert t.status == TargetStatus.PUBLISHED and sends.call_count == 2
    body = json.loads(sends.calls[0].request.content)
    assert body["template"]["name"] == "news"
    assert body["template"]["components"][0]["parameters"][0]["text"] == "Big news today"

    # Webhook: delivery statuses, JOIN and STOP, signed with the Meta app secret.
    settings.META_APP_SECRET = "appsecret"
    conf.invalidate()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "111"},
                            "statuses": [{"id": "wamid.0", "status": "delivered"}, {"id": "wamid.1", "status": "read"}],
                            "contacts": [{"wa_id": "393330000009", "profile": {"name": "Neo"}}],
                            "messages": [
                                {"from": "393330000009", "text": {"body": "join"}},
                                {"from": "393330000001", "text": {"body": "STOP"}},
                            ],
                        }
                    }
                ]
            }
        ]
    }
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"appsecret", raw, hashlib.sha256).hexdigest()
    anon = APIClient()
    assert (
        anon.post(
            "/api/channels/whatsapp/webhook/",
            raw,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=bad",
        ).status_code
        == 401
    )
    assert anon.post(
        "/api/channels/whatsapp/webhook/", raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig
    ).json() == {"handled": 4}
    assert set(Delivery.objects.values_list("status", flat=True)) == {"delivered", "read"}
    assert Subscriber.objects.get(address="393330000009").source == "keyword"
    assert Subscriber.objects.get(address="393330000001").opted_out_at is not None
    m = WhatsAppProvider(ch).fetch_metrics([t.external_id])[t.external_id]
    assert (m.impressions, m.reach) == (2, 1)
    # Meta's verification handshake.
    r = anon.get(
        "/api/channels/whatsapp/webhook/",
        {"hub.mode": "subscribe", "hub.verify_token": "vt-123", "hub.challenge": "42"},
    )
    assert r.content == b"42"
    assert (
        anon.get(
            "/api/channels/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "42"}
        ).status_code
        == 403
    )


def test_subscriber_api_requires_consent(api, workspace):
    ch = wa_channel(workspace)
    url = f"/api/channels/{ch.id}/subscribers/"
    assert api.post(url, {"address": "+39 333 1234567"}, format="json").status_code == 400
    assert (
        api.post(url, {"address": "+39 333 1234567", "name": "Ada", "consent": True}, format="json").status_code == 201
    )
    r = api.post(url, {"lines": "+44 7700 900123, Bob\nnot-a-number", "consent": True}, format="json")
    assert r.data == {"added": 1, "invalid": ["not-a-number"]}
    rows = api.get(url).data["results"]
    assert {s["address"] for s in rows} == {"393331234567", "447700900123"}
    assert api.get("/api/channels/").data["results"][0]["setup"]["subscribers"] == 2


# --- QQ ---------------------------------------------------------------------------------


@pytest.fixture
def qq_token():
    qqmod._token_cache.clear()
    respx.post(qqmod.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "qtok", "expires_in": "7200"})
    )


@respx.mock
def test_qq_connect_lists_guild_channels_and_known_groups(qq_token):
    respx.get(f"{qqmod.API}/users/@me").mock(return_value=httpx.Response(200, json={"username": "MarketteBot"}))
    respx.get(f"{qqmod.API}/users/@me/guilds").mock(
        return_value=httpx.Response(200, json=[{"id": "g1", "name": "Acme"}])
    )
    respx.get(f"{qqmod.API}/guilds/g1/channels").mock(
        return_value=httpx.Response(
            200, json=[{"id": "c1", "name": "news", "type": 0}, {"id": "c2", "name": "voice", "type": 2}]
        )
    )
    InboundChat.objects.create(provider="qq", app_id="app1", chat_id="GROUPOPENID")
    cands = QQProvider().connect_with_credentials({"app_id": "app1", "app_secret": "s"})
    assert [c.external_id for c in cands] == ["channel:c1", "group:GROUPOPENID"]
    assert respx.calls[1].request.headers["authorization"] == "QQBot qtok"


@respx.mock
def test_qq_group_publish_with_image_and_link_rule(workspace, qq_token, tmp_path):
    ch = Channel.create_for(
        workspace=workspace,
        provider="qq",
        external_id="group:G1",
        name="g",
        credentials={"app_id": "app1", "app_secret": "s"},
        config={"kind": "group"},
    )
    files = respx.post(f"{qqmod.API}/v2/groups/G1/files").mock(
        return_value=httpx.Response(200, json={"file_info": "FI"})
    )
    msgs = respx.post(f"{qqmod.API}/v2/groups/G1/messages").mock(return_value=httpx.Response(200, json={"id": "m9"}))
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    res = QQProvider(ch).publish(
        Content(text="大新闻", media=[MediaItem(path=Path(img), url="https://x/a.png", mime="image/png")])
    )
    assert res.external_id == "group:G1:m9"
    assert json.loads(files.calls.last.request.content)["url"] == "https://x/a.png"
    assert json.loads(msgs.calls.last.request.content) == {
        "msg_type": 7,
        "content": "大新闻",
        "media": {"file_info": "FI"},
        "msg_seq": 1,
    }
    assert QQProvider(ch).validate(Content(text="see https://x.cn")) == [
        "QQ does not allow links in group messages; remove them."
    ]
    msgs.mock(return_value=httpx.Response(400, json={"code": 40034105, "message": "主动消息失败, 无权限"}))
    with pytest.raises(ProviderError, match="group owner must enable"):
        QQProvider(ch).publish(Content(text="hi"))


def test_qq_webhook_validation_and_signed_events(db):
    from apps.channels.models import OAuthApp

    secret = "naOC0ocQE3shWLAfffVLB1rhYPG7"
    OAuthApp.objects.create(
        provider="qq", base_url="https://q.qq.com/bot/app1", client_id="app1", secrets={"client_secret": secret}
    )
    c = APIClient()
    r = c.post(
        "/api/channels/qq/webhook/app1/",
        {"op": 13, "d": {"plain_token": "Arq0D5A61EgUu4OxUvOp", "event_ts": "1725442341"}},
        format="json",
    )
    seed = (secret * 2)[:32].encode()
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    pub.verify(bytes.fromhex(r.json()["signature"]), b"1725442341Arq0D5A61EgUu4OxUvOp")  # raises if wrong
    event = {"op": 0, "t": "GROUP_ADD_ROBOT", "d": {"group_openid": "GX", "timestamp": 1}}
    raw = json.dumps(event).encode()
    sig = Ed25519PrivateKey.from_private_bytes(seed).sign(b"1700000000" + raw).hex()
    assert (
        c.post(
            "/api/channels/qq/webhook/app1/",
            raw,
            content_type="application/json",
            HTTP_X_SIGNATURE_TIMESTAMP="1700000000",
            HTTP_X_SIGNATURE_ED25519="00" * 64,
        ).status_code
        == 401
    )
    r = c.post(
        "/api/channels/qq/webhook/app1/",
        raw,
        content_type="application/json",
        HTTP_X_SIGNATURE_TIMESTAMP="1700000000",
        HTTP_X_SIGNATURE_ED25519=sig,
    )
    assert r.json() == {"op": 12} and InboundChat.objects.filter(chat_id="GX").exists()


# --- WeChat -----------------------------------------------------------------------------


@pytest.fixture
def wx_token():
    wxmod._token_cache.clear()
    respx.post(f"{wxmod.API}/stable_token").mock(
        return_value=httpx.Response(200, json={"access_token": "wtok", "expires_in": 7200})
    )


@respx.mock
def test_wechat_connect_and_ip_whitelist_hint(wx_token):
    respx.get(f"{wxmod.API}/user/get").mock(return_value=httpx.Response(200, json={"total": 5120, "count": 0}))
    [c] = WeChatProvider().connect_with_credentials({"app_id": "wx1", "app_secret": "s", "account_name": "Acme 公众号"})
    assert (c.name, c.followers_count, c.config["mode"]) == ("Acme 公众号", 5120, "push")
    respx.get(f"{wxmod.API}/user/get").mock(
        return_value=httpx.Response(200, json={"errcode": 40164, "errmsg": "invalid ip"})
    )
    with pytest.raises(ProviderError, match="IP whitelist") as exc:
        WeChatProvider().connect_with_credentials({"app_id": "wx1", "app_secret": "s"})
    assert exc.value.auth


@respx.mock
def test_wechat_push_and_publish_modes(workspace, wx_token, tmp_path):
    ch = Channel.create_for(
        workspace=workspace,
        provider="wechat",
        external_id="wx1",
        name="Acme",
        credentials={"app_id": "wx1", "app_secret": "s"},
        config={"mode": "push", "author": "Ada"},
    )
    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"jpg")
    respx.post(f"{wxmod.API}/material/add_material").mock(return_value=httpx.Response(200, json={"media_id": "THUMB"}))
    draft = respx.post(f"{wxmod.API}/draft/add").mock(return_value=httpx.Response(200, json={"media_id": "DRAFT"}))
    mass = respx.post(f"{wxmod.API}/message/mass/sendall").mock(
        return_value=httpx.Response(200, json={"errcode": 0, "msg_id": 1000001, "msg_data_id": 2000002})
    )
    content = Content(
        text="**你好** world",
        title="Launch",
        media=[MediaItem(path=Path(cover), url="https://x/c.jpg", mime="image/jpeg")],
    )
    res = WeChatProvider(ch).publish(content)
    article = json.loads(draft.calls.last.request.content)["articles"][0]
    assert article["thumb_media_id"] == "THUMB" and "<strong>你好</strong>" in article["content"]
    assert res.external_id == "mass:1000001"
    assert json.loads(mass.calls.last.request.content)["mpnews"] == {"media_id": "DRAFT"}
    respx.post(f"{wxmod.API}/freepublish/submit").mock(return_value=httpx.Response(200, json={"publish_id": "P1"}))
    respx.post(f"{wxmod.API}/freepublish/get").mock(
        return_value=httpx.Response(
            200,
            json={"publish_status": 0, "article_detail": {"item": [{"article_url": "https://mp.weixin.qq.com/s/abc"}]}},
        )
    )
    content.options = {"wechat_mode": "publish"}
    res = WeChatProvider(ch).publish(content)
    assert (res.external_id, res.url) == ("publish:P1", "https://mp.weixin.qq.com/s/abc")
    assert WeChatProvider(ch).validate(Content(text="x", title="t")) == ["WeChat requires at least one image or video."]


def test_subscribers_are_admin_only(workspace):
    from apps.core.models import User
    from apps.tenants.models import Membership

    ch = wa_channel(workspace)
    editor = User.objects.create_user("ed@example.com", "pw-pw-pw-pw-1")
    Membership.objects.create(workspace=workspace, user=editor, role="editor")
    c = APIClient()
    c.force_authenticate(editor)
    c.credentials(HTTP_X_WORKSPACE=workspace.slug)
    assert c.get(f"/api/channels/{ch.id}/subscribers/").status_code == 403


@respx.mock
def test_qq_without_chats_returns_callback_url(api, qq_token):
    respx.get(f"{qqmod.API}/users/@me").mock(return_value=httpx.Response(200, json={"username": "Bot"}))
    respx.get(f"{qqmod.API}/users/@me/guilds").mock(return_value=httpx.Response(200, json=[]))
    r = api.post(
        "/api/channels/connect/",
        {"provider": "qq", "credentials": {"app_id": "app9", "app_secret": "s"}},
        format="json",
    )
    assert r.status_code == 400 and r.data["callback_url"].endswith("/api/channels/qq/webhook/app9/")

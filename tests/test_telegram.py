import json
from pathlib import Path

import httpx
import pytest
import respx

from apps.channels.models import Channel
from apps.channels.providers import Content, MediaItem, ProviderError
from apps.channels.providers.telegram import API, TelegramProvider

TOKEN = "123:ABC"
BOT = f"{API}/bot{TOKEN}"
ME = {"id": 99, "is_bot": True, "username": "markette_bot"}
CHANNEL = {"id": -1001234567890, "type": "channel", "title": "Acme News", "username": "acmenews"}
GROUP = {"id": -1009876543210, "type": "supergroup", "title": "Acme Team"}


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def fail(code, description, **params):
    return httpx.Response(
        code, json={"ok": False, "error_code": code, "description": description, "parameters": params}
    )


def body(route):
    return json.loads(route.calls.last.request.content)


@respx.mock
def test_connect_named_channel_checks_admin_rights():
    respx.post(f"{BOT}/getMe").mock(return_value=ok(ME))
    respx.post(f"{BOT}/getChat").mock(return_value=ok(CHANNEL))
    member = respx.post(f"{BOT}/getChatMember").mock(
        return_value=ok({"status": "administrator", "can_post_messages": True})
    )
    respx.post(f"{BOT}/getChatMemberCount").mock(return_value=ok(1520))
    [c] = TelegramProvider().connect_with_credentials({"bot_token": TOKEN, "chat": "https://t.me/acmenews"})
    assert (c.external_id, c.name, c.handle, c.followers_count) == ("-1001234567890", "Acme News", "@acmenews", 1520)
    assert c.config["chat_type"] == "channel" and c.credentials == {"bot_token": TOKEN}
    member.mock(return_value=ok({"status": "member"}))
    with pytest.raises(ProviderError, match="administrator"):
        TelegramProvider().connect_with_credentials({"bot_token": TOKEN, "chat": "@acmenews"})


@respx.mock
def test_connect_discovers_recent_chats():
    respx.post(f"{BOT}/getMe").mock(return_value=ok(ME))
    respx.post(f"{BOT}/getUpdates").mock(
        return_value=ok(
            [
                {"update_id": 1, "my_chat_member": {"chat": CHANNEL}},
                {"update_id": 2, "message": {"chat": GROUP}},
                {"update_id": 3, "message": {"chat": {"id": 5, "type": "private"}}},
            ]
        )
    )
    respx.post(f"{BOT}/getChat").mock(
        side_effect=lambda req: ok(CHANNEL if json.loads(req.content)["chat_id"] == CHANNEL["id"] else GROUP)
    )
    respx.post(f"{BOT}/getChatMember").mock(return_value=ok({"status": "administrator", "can_post_messages": True}))
    respx.post(f"{BOT}/getChatMemberCount").mock(return_value=ok(10))
    cands = TelegramProvider().connect_with_credentials({"bot_token": TOKEN})
    assert {c.name for c in cands} == {"Acme News", "Acme Team"}


@respx.mock
def test_bad_token_is_an_auth_error():
    respx.post(f"{BOT}/getMe").mock(return_value=fail(401, "Unauthorized"))
    with pytest.raises(ProviderError) as exc:
        TelegramProvider().connect_with_credentials({"bot_token": TOKEN})
    assert exc.value.auth


@pytest.fixture
def channel_row(workspace):
    return Channel(
        organization=workspace.organization,
        provider="telegram",
        external_id=str(CHANNEL["id"]),
        name="Acme News",
        credentials={"bot_token": TOKEN},
        config={"chat_type": "channel", "username": "acmenews"},
    )


@respx.mock
def test_text_post_with_options_and_pin(channel_row):
    send = respx.post(f"{BOT}/sendMessage").mock(return_value=ok({"message_id": 42, "chat": CHANNEL}))
    pin = respx.post(f"{BOT}/pinChatMessage").mock(return_value=ok(True))
    res = TelegramProvider(channel_row).publish(
        Content(text="Hello subscribers", options={"silent": True, "pin": True, "disable_link_preview": True})
    )
    assert (res.external_id, res.url) == ("-1001234567890:42", "https://t.me/acmenews/42")
    sent = body(send)
    assert sent["disable_notification"] is True and sent["link_preview_options"] == {"is_disabled": True}
    assert body(pin)["message_id"] == 42


@respx.mock
def test_photo_album_and_long_text_follow_up(channel_row, tmp_path):
    imgs = []
    for i in range(2):
        p = tmp_path / f"p{i}.jpg"
        p.write_bytes(b"\xff\xd8 fake")
        imgs.append(MediaItem(path=Path(p), url=f"https://x/p{i}.jpg", mime="image/jpeg", filename=f"p{i}.jpg"))
    album = respx.post(f"{BOT}/sendMediaGroup").mock(
        return_value=ok([{"message_id": 7, "chat": {**CHANNEL, "username": None}}, {"message_id": 8}])
    )
    follow = respx.post(f"{BOT}/sendMessage").mock(return_value=ok({"message_id": 9}))
    res = TelegramProvider(channel_row).publish(Content(text="x" * 1500, media=imgs))
    req = album.calls.last.request.content.decode(errors="ignore")
    assert "attach://file0" in req and "attach://file1" in req and '"caption"' not in req
    assert len(body(follow)["text"]) == 1500
    assert res.url == "https://t.me/c/1234567890/7"


@respx.mock
def test_rate_limit_and_removed_bot(channel_row):
    route = respx.post(f"{BOT}/sendMessage").mock(return_value=fail(429, "Too Many Requests", retry_after=17))
    with pytest.raises(ProviderError) as exc:
        TelegramProvider(channel_row).publish(Content(text="hi"))
    assert exc.value.retryable and exc.value.retry_after == 17
    route.mock(return_value=fail(403, "Forbidden: bot was kicked from the channel chat"))
    with pytest.raises(ProviderError) as exc:
        TelegramProvider(channel_row).publish(Content(text="hi"))
    assert exc.value.auth

from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from apps.channels.models import Channel
from apps.channels.providers import Content
from apps.channels.providers import telegram_user as tu
from apps.inbox import tasks as inbox_tasks
from apps.inbox.models import InboxItem
from apps.publishing.models import PostTarget, TargetStatus

pytestmark = pytest.mark.django_db(transaction=True)


class Errors:
    class RPCError(Exception):
        pass

    class FloodWaitError(RPCError):
        seconds = 30

    class AuthKeyUnregisteredError(RPCError):
        pass

    class SessionRevokedError(RPCError):
        pass

    class UserDeactivatedError(RPCError):
        pass

    class SessionPasswordNeededError(RPCError):
        pass

    class PhoneCodeInvalidError(RPCError):
        pass

    class PhoneCodeExpiredError(RPCError):
        pass

    class PasswordHashInvalidError(RPCError):
        pass


ME = NS(id=7, username="ada", first_name="Ada", last_name=None, is_self=True, bot=False)
BOB = NS(id=42, username="bob", first_name="Bob", last_name="B", is_self=False, bot=False)
NEWS = NS(id=-1001, username="adanews", title="Ada news", creator=True, admin_rights=None, participants_count=900)


def msg(mid, text, *, out=False, sender=BOB, chat_id=42, reply_to=None, mentioned=False):
    return NS(
        id=mid,
        message=text,
        out=out,
        sender=sender,
        chat_id=chat_id,
        media=None,
        mentioned=mentioned,
        reply_to=NS(reply_to_msg_id=reply_to) if reply_to else None,
        date=datetime(2026, 10, 21, 10, mid % 60, tzinfo=UTC),
        views=120,
        forwards=3,
        replies=NS(replies=2),
        reactions=NS(results=[NS(count=5), NS(count=1)]),
    )


class World:
    """What the fake Telegram knows and what was done."""

    def __init__(self):
        self.sent, self.edited, self.password = [], [], None
        self.history = {42: [msg(11, "Hi Ada!"), msg(12, "Are you there?")], -1001: []}

    def dialogs(self):
        return [
            NS(
                id=42,
                entity=BOB,
                name="Bob B",
                is_user=True,
                is_group=False,
                is_channel=False,
                message=self.history[42][-1],
            ),
            NS(id=-1001, entity=NEWS, name="Ada news", is_user=False, is_group=False, is_channel=True, message=None),
        ]


@pytest.fixture
def world(monkeypatch):
    w = World()

    class Session:
        def __init__(self, s=None):
            self.s = s or "fresh"

        def save(self):
            return self.s + "+saved"

    class Client:
        def __init__(self, session, api_id, api_hash):
            self.session = session

        def connect(self):
            pass

        def disconnect(self):
            pass

        def send_code_request(self, phone):
            return NS(phone_code_hash="hash1")

        def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
            if password is not None:
                if password != "secret":
                    raise Errors.PasswordHashInvalidError()
                return ME
            if code != "12345":
                raise Errors.PhoneCodeInvalidError()
            raise Errors.SessionPasswordNeededError()

        def get_me(self):
            return ME

        def iter_dialogs(self, limit=None):
            return w.dialogs()

        def iter_messages(self, entity, min_id=0, limit=None):
            key = getattr(entity, "id", entity)
            return [m for m in reversed(w.history.get(key, [])) if m.id > min_id]

        def send_message(self, chat, text, reply_to=None, link_preview=True):
            m = msg(100 + len(w.sent), text, out=True, chat_id=chat)
            w.sent.append((chat, text, reply_to))
            return m

        def edit_message(self, chat, mid, text):
            w.edited.append((chat, mid, text))

        def get_messages(self, chat, ids):
            return [msg(i, "x", out=True) for i in ids]

    monkeypatch.setattr(tu, "_telethon", lambda: (Client, Session, Errors))
    return w


def test_sign_in_with_two_step_password_and_pick_chats(api, workspace, world):
    r = api.post(
        "/api/channels/telegram-login/",
        {"step": "start", "api_id": "123", "api_hash": "abc", "phone": "+39 333 1234567"},
        format="json",
    )
    assert r.status_code == 200, r.data
    rid = r.data["request_id"]
    r = api.post("/api/channels/telegram-login/", {"step": "code", "request_id": rid, "code": "999"}, format="json")
    assert r.status_code == 400 and "not right" in str(r.data)
    r = api.post("/api/channels/telegram-login/", {"step": "code", "request_id": rid, "code": "12345"}, format="json")
    assert r.status_code == 400 and r.data["needs"] == "password"
    r = api.post(
        "/api/channels/telegram-login/", {"step": "code", "request_id": rid, "password": "secret"}, format="json"
    )
    assert r.status_code == 200 and r.data["count"] == 3
    picked = api.get(f"/api/channels/connections/{rid}/").data
    names = [c["name"] for c in picked["candidates"]]
    assert names == ["Direct messages · @ada", "Bob B", "Ada news"] and "credentials" not in str(picked)
    r = api.post(f"/api/channels/connections/{rid}/", {"external_ids": ["dm:7", "-1001"]}, format="json")
    assert r.status_code in (200, 201), r.data
    news = Channel.objects.get(provider="telegram_user", external_id="-1001")
    assert news.credentials["session"].startswith("fresh+saved") and news.credentials["api_id"] == 123


def connected(workspace):
    creds = {"api_id": 123, "api_hash": "abc", "session": "s", "user_id": 7}
    dm = Channel.create_for(
        workspace=workspace,
        provider="telegram_user",
        external_id="dm:7",
        name="DMs",
        credentials=creds,
        config={"dm": True},
    )
    news = Channel.create_for(
        workspace=workspace,
        provider="telegram_user",
        external_id="-1001",
        name="Ada news",
        credentials=creds,
        config={"username": "adanews", "broadcast": True},
    )
    return dm, news


def test_publish_edit_and_views_as_the_account(api, workspace, world):
    dm, news = connected(workspace)
    post = api.post("/api/posts/", {"body": "Hello channel", "targets": [{"channel": news.id}]}, format="json").data
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    t = PostTarget.objects.get(channel=news)
    assert t.status == TargetStatus.PUBLISHED and t.external_url == "https://t.me/adanews/100"
    assert world.sent == [(-1001, "Hello channel", None)]
    api.post(f"/api/targets/{t.id}/edit-remote/", {"body": "Hello again"}, format="json")
    assert world.edited == [(-1001, 100, "Hello again")]
    m = news.get_provider().fetch_metrics([t.external_id])[t.external_id]
    assert (m.impressions, m.shares, m.comments, m.likes) == (120, 3, 2, 6)
    r = api.post("/api/posts/", {"body": "x", "targets": [{"channel": dm.id}]}, format="json").data
    assert api.post(f"/api/posts/{r['id']}/schedule/", {"mode": "now"}, format="json").status_code == 400


def test_direct_messages_reach_the_inbox_and_get_answers(api, workspace, world):
    dm, _ = connected(workspace)
    assert inbox_tasks.poll_channel(dm.pk) == 2
    items = api.get("/api/inbox/").data["results"]
    assert [i["text"] for i in items] == ["Are you there?", "Hi Ada!"] and items[0]["author_handle"] == "@bob"
    assert items[0]["kind"] == "message" and items[0]["can_reply"]
    r = api.post(f"/api/inbox/{items[0]['id']}/reply/", {"text": "Yes!"}, format="json")
    assert r.status_code == 200 and world.sent[-1] == (42, "Yes!", 12)
    # Already seen: nothing twice.
    from apps.inbox.models import InboxState

    InboxState.objects.update(next_poll_at=None)
    world.history[42].append(msg(13, "Thanks"))
    assert inbox_tasks.poll_channel(dm.pk) == 1 and InboxItem.objects.count() == 3


def test_errors_are_mapped(world):
    e = tu._error(Errors.FloodWaitError())
    assert e.retryable and e.retry_after == 30
    assert tu._error(Errors.AuthKeyUnregisteredError()).auth
    assert "Telegram" in str(tu._error(Errors.PhoneCodeInvalidError()))


def test_dm_channels_refuse_publishing(workspace, world):
    dm, _ = connected(workspace)
    assert "direct messages" in dm.get_provider().validate(Content(text="x"))[0]

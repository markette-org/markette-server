import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from apps.channels.models import Channel
from apps.channels.providers import Content, MediaItem, ProviderError
from apps.channels.providers import slack as sl
from apps.channels.providers.slack import SlackProvider, escape
from apps.platform import conf

pytestmark = pytest.mark.django_db

API = sl.API
TEAM = {"ok": True, "team_id": "T1", "team": "Acme", "url": "https://acme.slack.com/", "user_id": "U1"}
CHANNELS = [
    {"id": "C2", "name": "news", "is_private": False, "is_member": False, "num_members": 40},
    {"id": "C1", "name": "general", "is_private": False, "is_member": True, "num_members": 50},
    {"id": "G1", "name": "secret", "is_private": True, "is_member": False},
    {"id": "G2", "name": "team", "is_private": True, "is_member": True, "num_members": 5},
]


def ok(**body):
    return httpx.Response(200, json={"ok": True, **body})


def mock_workspace(channels=CHANNELS):
    respx.post(f"{API}/auth.test").mock(return_value=httpx.Response(200, json=TEAM))
    respx.get(f"{API}/team.info").mock(return_value=ok(team={"icon": {"image_132": "https://icon"}}))
    return respx.get(f"{API}/conversations.list").mock(return_value=ok(channels=channels))


def slack_channel(workspace, private=False):
    return Channel(
        organization=workspace.organization,
        provider="slack",
        external_id="C1",
        name="#general",
        credentials={"bot_token": "xoxb-1"},
        config={"team_id": "T1", "private": private},
    )


def test_escape():
    assert escape("a < b & c > d https://x.example/?a=1&b=2") == (
        "a &lt; b &amp; c &gt; d https://x.example/?a=1&amp;b=2"
    )


@pytest.fixture
def slack_oauth(settings):
    settings.SLACK_CLIENT_ID, settings.SLACK_CLIENT_SECRET = "cid", "csecret"
    conf.invalidate()
    yield
    settings.SLACK_CLIENT_ID = settings.SLACK_CLIENT_SECRET = ""
    conf.invalidate()


def test_auth_mode_depends_on_server_app(settings, slack_oauth):
    desc = SlackProvider.describe()
    assert desc["auth"] == "oauth2" and desc["credential_fields"] == [] and desc["configured"]
    settings.SLACK_CLIENT_ID = ""
    conf.invalidate()
    desc = SlackProvider.describe()
    assert desc["auth"] == "credentials" and [f["name"] for f in desc["credential_fields"]] == ["bot_token", "channel"]


@respx.mock
def test_credentials_list_postable_channels():
    mock_workspace()
    cands = SlackProvider().connect_with_credentials({"bot_token": " xoxb-1 "})
    # Archived and private channels without the app are left out; sorted by name.
    assert [c.name for c in cands] == ["#general", "#news", "#team"]
    general = cands[0]
    assert general.external_id == "C1" and general.handle == "Acme"
    assert general.profile_url == "https://acme.slack.com/archives/C1" and general.avatar_url == "https://icon"
    assert general.credentials == {"bot_token": "xoxb-1"} and general.followers_count == 50
    assert cands[2].handle == "Acme · private" and cands[2].config["private"] is True


def test_credentials_reject_other_tokens():
    with pytest.raises(ProviderError, match="xoxb-"):
        SlackProvider().connect_with_credentials({"bot_token": "xoxp-user"})


@respx.mock
def test_credentials_with_channel_name_link_or_id():
    mock_workspace()
    info = respx.get(f"{API}/conversations.info").mock(return_value=ok(channel=CHANNELS[0]))
    [c] = SlackProvider().connect_with_credentials({"bot_token": "xoxb-1", "channel": "#News"})
    assert c.external_id == "C2"
    [c] = SlackProvider().connect_with_credentials(
        {"bot_token": "xoxb-1", "channel": "https://acme.slack.com/archives/C2ABCDEFG"}
    )
    assert info.calls.last.request.url.params["channel"] == "C2ABCDEFG"
    with pytest.raises(ProviderError, match="Add the app to #secret"):
        SlackProvider().connect_with_credentials({"bot_token": "xoxb-1", "channel": "secret"})
    with pytest.raises(ProviderError, match="No channel named #nope"):
        SlackProvider().connect_with_credentials({"bot_token": "xoxb-1", "channel": "nope"})


@respx.mock
def test_bad_token_is_an_auth_error():
    respx.post(f"{API}/auth.test").mock(return_value=httpx.Response(200, json={"ok": False, "error": "invalid_auth"}))
    with pytest.raises(ProviderError) as exc:
        SlackProvider().connect_with_credentials({"bot_token": "xoxb-bad"})
    assert exc.value.auth


@respx.mock
def test_oauth_flow(slack_oauth):
    url = SlackProvider().authorization_url(SimpleNamespace(state="st"))
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["cid"] and "chat:write.public" in q["scope"][0].split(",")
    assert q["redirect_uri"][0].endswith("/api/oauth/slack/callback") and q["state"] == ["st"]
    token = respx.post(f"{API}/oauth.v2.access").mock(return_value=ok(access_token="xoxb-new", token_type="bot"))
    mock_workspace(CHANNELS[1:2])
    [c] = SlackProvider().complete_oauth(SimpleNamespace(), {"code": "abc"})
    assert c.credentials == {"bot_token": "xoxb-new"}
    form = parse_qs(token.calls.last.request.content.decode())
    assert form["code"] == ["abc"] and form["client_secret"] == ["csecret"]


@respx.mock
def test_publish_text_joins_public_channel_when_needed(workspace):
    post = respx.post(f"{API}/chat.postMessage").mock(
        side_effect=[
            httpx.Response(200, json={"ok": False, "error": "not_in_channel"}),
            ok(ts="171.01", channel="C1"),
        ]
    )
    join = respx.post(f"{API}/conversations.join").mock(return_value=ok())
    respx.get(f"{API}/chat.getPermalink").mock(return_value=ok(permalink="https://acme.slack.com/archives/C1/p17101"))
    res = SlackProvider(slack_channel(workspace)).publish(Content(text="Hi <team> & co", link="https://acme.org"))
    assert (res.external_id, res.url) == ("C1:171.01", "https://acme.slack.com/archives/C1/p17101")
    body = json.loads(post.calls.last.request.content)
    assert body["text"] == "Hi &lt;team&gt; &amp; co\nhttps://acme.org" and body["unfurl_links"] is True
    assert json.loads(join.calls.last.request.content) == {"channel": "C1"}
    assert post.calls.last.request.headers["authorization"] == "Bearer xoxb-1"


@respx.mock
def test_publish_private_channel_without_app_needs_reauth(workspace):
    respx.post(f"{API}/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
    )
    with pytest.raises(ProviderError) as exc:
        SlackProvider(slack_channel(workspace, private=True)).publish(Content(text="Hi"))
    assert exc.value.auth


@respx.mock
def test_publish_with_images(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(sl.time, "sleep", lambda s: None)
    p = tmp_path / "a.png"
    p.write_bytes(b"png-bytes")
    item = MediaItem(path=Path(p), url="https://m/a.png", mime="image/png", filename="a.png", alt="A chart")
    slots = respx.post(f"{API}/files.getUploadURLExternal").mock(
        return_value=ok(upload_url="https://files.slack.com/upload/v1/x", file_id="F1")
    )
    upload = respx.post("https://files.slack.com/upload/v1/x").mock(return_value=httpx.Response(200, text="OK"))
    complete = respx.post(f"{API}/files.completeUploadExternal").mock(return_value=ok(files=[{"id": "F1"}]))
    respx.get(f"{API}/files.info").mock(
        side_effect=[ok(file={"shares": {}}), ok(file={"shares": {"public": {"C1": [{"ts": "172.5"}]}}})]
    )
    respx.get(f"{API}/chat.getPermalink").mock(return_value=ok(permalink="https://acme.slack.com/archives/C1/p1725"))
    res = SlackProvider(slack_channel(workspace)).publish(Content(text="Look", media=[item]))
    assert res.external_id == "C1:172.5" and res.url.endswith("p1725")
    form = parse_qs(slots.calls.last.request.content.decode())
    assert form == {"filename": ["a.png"], "length": ["9"], "alt_txt": ["A chart"]}
    assert b"png-bytes" in upload.calls.last.request.content
    assert json.loads(complete.calls.last.request.content) == {
        "files": [{"id": "F1"}],
        "channel_id": "C1",
        "initial_comment": "Look",
    }


@respx.mock
def test_rate_limit_is_retryable(workspace):
    respx.post(f"{API}/chat.postMessage").mock(return_value=httpx.Response(429, headers={"retry-after": "7"}))
    with pytest.raises(ProviderError) as exc:
        SlackProvider(slack_channel(workspace)).publish(Content(text="Hi"))
    assert exc.value.retryable and exc.value.retry_after == 7


@respx.mock
def test_delete_message_or_files(workspace):
    delete = respx.post(f"{API}/chat.delete").mock(return_value=ok())
    files = respx.post(f"{API}/files.delete").mock(return_value=ok())
    provider = SlackProvider(slack_channel(workspace))
    provider.delete("C1:171.01")
    assert json.loads(delete.calls.last.request.content) == {"channel": "C1", "ts": "171.01"}
    provider.delete("C1:files:F1,F2")
    assert files.call_count == 2


@respx.mock
def test_connect_endpoint_creates_channels(api):
    mock_workspace(CHANNELS[1:2])
    res = api.post(
        "/api/channels/connect/",
        {"provider": "slack", "credentials": {"bot_token": "xoxb-1"}},
        format="json",
    )
    assert res.status_code == 201, res.data
    ch = Channel.objects.get(provider="slack")
    assert ch.name == "#general" and ch.credentials == {"bot_token": "xoxb-1"}
    providers = {p["key"]: p for p in api.get("/api/providers/").data}
    assert providers["slack"]["auth"] == "credentials"

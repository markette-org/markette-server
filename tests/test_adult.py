import json

import httpx
import pytest
import respx
from django.core.files.base import ContentFile

from apps.channels.models import Channel
from apps.channels.providers import Content, MediaItem
from apps.channels.providers import uploader as up
from apps.channels.providers.adult import (
    FanslyProvider,
    OnlyFansProvider,
    PornhubProvider,
    RedTubeProvider,
    XHamsterProvider,
    YouPornProvider,
)
from apps.publishing import tasks as ptasks
from apps.publishing.models import MediaAsset, PostTarget, TargetStatus

pytestmark = pytest.mark.django_db(transaction=True)
HOOK = "https://hooks.example/n"


def turn_on(api, workspace):
    r = api.patch(f"/api/workspaces/{workspace.id}/", {"adult_content": True}, format="json")
    assert r.status_code == 400 and "18 or older" in str(r.data)
    r = api.patch(
        f"/api/workspaces/{workspace.id}/", {"adult_content": True, "adult_confirmation": True}, format="json"
    )
    assert r.status_code == 200 and r.data["adult_content"] and r.data["adult_confirmed_at"]


def connect(api, provider="pornhub", **creds):
    return api.post(
        "/api/channels/connect/",
        {"provider": provider, "credentials": {"profile": "https://www.pornhub.com/model/jane-doe", **creds}},
        format="json",
    )


def video(workspace):
    asset = MediaAsset(workspace=workspace, mime="video/mp4", original_name="clip.mp4", size=3)
    asset.file.save("clip.mp4", ContentFile(b"mp4"), save=True)
    return asset


def test_adult_platforms_need_the_workspace_switch(api, workspace):
    r = connect(api)
    assert r.status_code == 400 and "Adult platforms are off" in str(r.data)
    turn_on(api, workspace)
    r = connect(api)
    assert r.status_code == 201, r.data
    ch = Channel.objects.get(provider="pornhub")
    assert ch.external_id == "jane-doe" and ch.config == {"upload_mode": "handoff"}
    workspace.adult_content = False
    workspace.save()
    assert all(c["provider"] != "pornhub" for c in api.get("/api/channels/").data["results"])
    r = api.post("/api/posts/", {"body": "x", "targets": [{"channel": ch.id}]}, format="json")
    assert r.status_code == 400 and "Adult platforms are off" in str(r.data)


@respx.mock
def test_handoff_then_uploaded_by_hand(api, workspace):
    turn_on(api, workspace)
    connect(api)
    ch = Channel.objects.get(provider="pornhub")
    api.post(
        "/api/notifications/",
        {"kind": "webhook", "name": "h", "config": {"url": HOOK}, "events": ["publication.handoff"]},
        format="json",
    )
    hook = respx.post(HOOK).mock(return_value=httpx.Response(200))
    asset = video(workspace)
    post = api.post(
        "/api/posts/",
        {
            "title": "New scene",
            "body": "Description",
            "tags": ["tag1"],
            "media_ids": [asset.id],
            "targets": [{"channel": ch.id}],
        },
        format="json",
    ).data
    r = api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    assert r.status_code == 200, r.data
    t = PostTarget.objects.get(post_id=post["id"])
    assert t.status == TargetStatus.HANDOFF and "Upload it on Pornhub" in t.last_error
    h = t.extra["handoff"]
    assert h["upload_url"].startswith("https://www.pornhub.com/upload") and h["title"] == "New scene"
    assert h["tags"] == ["tag1"] and h["media"][0]["url"].endswith(".mp4")
    assert api.get(f"/api/posts/{post['id']}/").data["status"] == "handoff"
    sent = json.loads(hook.calls.last.request.content)
    assert sent["event"] == "publication.handoff" and sent["data"]["handoff"]["site"] == "Pornhub"
    # Uploaded: the address gives the video's id, so views are collected.
    r = api.post(f"/api/targets/{t.id}/resolve/", {"published": True}, format="json")
    assert r.status_code == 400
    url = "https://www.pornhub.com/view_video.php?viewkey=6712abc"
    r = api.post(f"/api/targets/{t.id}/resolve/", {"published": True, "url": url}, format="json")
    assert r.status_code == 200 and r.data["status"] == "published"
    t.refresh_from_db()
    assert t.external_id == "6712abc" and t.next_metrics_at is not None


def test_not_uploading_cancels_and_tells_followers(api, workspace, fake_provider):
    turn_on(api, workspace)
    connect(api, "onlyfans", profile="https://onlyfans.com/jane")
    of = Channel.objects.get(provider="onlyfans")
    social = Channel.create_for(workspace=workspace, provider="fake", external_id="s", name="Social")
    post = api.post(
        "/api/posts/",
        {"body": "Teaser", "targets": [{"channel": of.id}, {"channel": social.id, "follows_channel": of.id}]},
        format="json",
    ).data
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    t = PostTarget.objects.get(channel=of)
    assert t.status == TargetStatus.HANDOFF
    api.post(f"/api/targets/{t.id}/resolve/", {"published": False}, format="json")
    t.refresh_from_db()
    follower = PostTarget.objects.get(channel=social)
    assert t.status == TargetStatus.CANCELLED and follower.status == TargetStatus.FAILED


def test_bot_mode_routes_to_the_uploader_or_hands_off(api, workspace, settings, monkeypatch):
    turn_on(api, workspace)
    cookies = json.dumps([{"name": "il", "value": "abc", "domain": ".pornhub.com", "sameSite": "no_restriction"}])
    connect(api, bot_cookies=cookies)
    ch = Channel.objects.get(provider="pornhub")
    assert ch.config["upload_mode"] == "bot" and ch.credentials["bot_cookies"][0]["sameSite"] == "None"
    state = api.get(f"/api/channels/{ch.id}/upload-bot/").data
    assert state["has_cookies"] and "bot_cookies" not in json.dumps(state) and not state["installed"]
    asset = video(workspace)
    body = {"title": "T", "body": "d", "media_ids": [asset.id], "targets": [{"channel": ch.id}]}
    # No uploader on this server: straight to the hand-off, saying why.
    post = api.post("/api/posts/", body, format="json").data
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    t = PostTarget.objects.get(post_id=post["id"])
    assert t.status == TargetStatus.HANDOFF and "bot is not running" in t.last_error
    # With the uploader installed, the publication goes to its queue instead.
    settings.UPLOAD_BOT_ENABLED = True
    from apps.platform import conf

    conf.invalidate()
    queued = []
    original = ptasks.publish_target.apply_async

    def apply_async(args=None, kwargs=None, queue=None, **options):
        if queue == "uploads":
            return queued.append((args, queue))
        return original(args=args, kwargs=kwargs, **options)

    monkeypatch.setattr(ptasks.publish_target, "apply_async", apply_async)
    post = api.post("/api/posts/", body, format="json").data
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    t = PostTarget.objects.get(post_id=post["id"])
    assert queued == [([t.id], "uploads")] and t.status == TargetStatus.QUEUED
    settings.UPLOAD_BOT_ENABLED = False
    conf.invalidate()


def test_parse_cookies_and_post_ids():
    assert up.parse_cookies("a=1; b=2", "onlyfans.com") == [
        {"name": "a", "value": "1", "domain": ".onlyfans.com", "path": "/"},
        {"name": "b", "value": "2", "domain": ".onlyfans.com", "path": "/"},
    ]
    assert PornhubProvider().post_id("https://www.pornhub.com/view_video.php?viewkey=ph5f1") == "ph5f1"
    assert YouPornProvider().post_id("https://www.youporn.com/watch/123/some-title/") == "123"
    assert RedTubeProvider().post_id("https://www.redtube.com/190753791") == "190753791"
    assert XHamsterProvider().post_id("https://xhamster.com/videos/a-title-xhAbc12") == "a-title-xhAbc12"
    assert OnlyFansProvider().post_id("https://onlyfans.com/123456/jane") == "123456"
    assert FanslyProvider().post_id("https://fansly.com/post/987") == "987"


@respx.mock
def test_redtube_views(workspace):
    ch = Channel(organization=workspace.organization, provider="redtube", external_id="jane", credentials={})
    respx.get("https://api.redtube.com/").mock(
        return_value=httpx.Response(200, json={"video": {"video_id": "1", "views": 2081282, "rating": "94.6"}})
    )
    assert RedTubeProvider(ch).fetch_metrics(["1"])["1"].impressions == 2081282


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def count(self):
        return 0 if self.selector in self.page.missing else 1

    @property
    def first(self):
        return self

    def set_input_files(self, files):
        self.page.actions.append(("file", files))

    def fill(self, value):
        self.page.actions.append(("fill", self.selector, value))

    def click(self):
        self.page.actions.append(("click", self.selector))
        self.page.url = "https://www.pornhub.com/view_video.php?viewkey=ph99"

    def evaluate_all(self, js):
        return []


class FakePage:
    def __init__(self, missing=()):
        self.url, self.actions, self.missing = "", [], set(missing)

    def set_default_timeout(self, ms):
        pass

    def goto(self, url, wait_until=None):
        self.url = url

    def locator(self, selector):
        return FakeLocator(self, selector)

    def screenshot(self, **kw):
        pass


def fake_playwright(page):
    class Ctx:
        def add_cookies(self, cookies):
            page.cookies = cookies

        def new_page(self):
            return page

    class Browser:
        def new_context(self, **kw):
            return Ctx()

        def close(self):
            pass

    class PW:
        chromium = type("C", (), {"launch": staticmethod(lambda: Browser())})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda: PW()


def test_bot_recipe_runs_and_falls_back(workspace, tmp_path, monkeypatch):
    ch = Channel(
        organization=workspace.organization,
        provider="pornhub",
        external_id="jane",
        credentials={"bot_cookies": [{"name": "il", "value": "x"}]},
        config={"upload_mode": "bot"},
    )
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x")
    content = Content(text="Desc", title="Title", tags=["a", "b"], media=[MediaItem(path=p, url="u", mime="video/mp4")])
    page = FakePage()
    monkeypatch.setattr(up, "_browser", lambda provider: fake_playwright(page))
    result = up.run(PornhubProvider(ch), content)
    assert result["url"].endswith("viewkey=ph99")
    assert ("fill", up.TITLE, "Title") in page.actions and ("fill", up.TAGS, "a, b") in page.actions
    assert page.actions[0] == ("file", [p]) and page.cookies == [{"name": "il", "value": "x"}]
    # Signed out: hand-off, nothing was sent.
    page = FakePage(missing={up.RECIPES["pornhub"][1]["logged_in"]})
    monkeypatch.setattr(up, "_browser", lambda provider: fake_playwright(page))
    with pytest.raises(up.HandoffRequired) as exc:
        up.run(PornhubProvider(ch), content)
    assert "not signed in" in str(exc.value) and not exc.value.maybe_uploaded and page.actions == []
    # A test run only looks.
    page = FakePage(missing={up.TAGS})
    monkeypatch.setattr(up, "_browser", lambda provider: fake_playwright(page))
    report = up.run(PornhubProvider(ch), dry_run=True)["report"]
    assert page.actions == [] and any(r.get("found") is False for r in report)


def test_more_post_ids():
    from apps.channels.providers.adult import (
        Clips4SaleProvider,
        EpornerProvider,
        ManyVidsProvider,
        SpankBangProvider,
        XnxxProvider,
        XVideosProvider,
    )

    assert XVideosProvider().post_id("https://www.xvideos.com/video.okhpuvd1234/my_clip") == "okhpuvd1234"
    assert XnxxProvider().post_id("https://www.xnxx.com/video-1abc2/my_clip") == "1abc2"
    assert SpankBangProvider().post_id("https://spankbang.com/8xyz/video/my+clip") == "8xyz"
    assert EpornerProvider().post_id("https://www.eporner.com/video-jAU0fEPoVX8/title/") == "jAU0fEPoVX8"
    assert ManyVidsProvider().post_id("https://www.manyvids.com/Video/5551234/my-clip") == "5551234"
    assert Clips4SaleProvider().post_id("https://www.clips4sale.com/studio/123456/7654321/my-clip") == "7654321"
    assert Clips4SaleProvider().username_from("https://www.clips4sale.com/studio/123456/my-studio") == "123456"
    assert ManyVidsProvider().username_from("https://www.manyvids.com/Profile/1234567/jane/") == "jane"


@respx.mock
def test_eporner_views(workspace):
    from apps.channels.providers.adult import EpornerProvider

    ch = Channel(organization=workspace.organization, provider="eporner", external_id="jane", credentials={})
    respx.get("https://www.eporner.com/api/v2/video/id/").mock(
        return_value=httpx.Response(200, json={"id": "abc", "views": 798, "rate": "3.75"})
    )
    assert EpornerProvider(ch).fetch_metrics(["abc"])["abc"].impressions == 798


@pytest.fixture
def fanvue_app(settings):
    from apps.platform import conf

    settings.FANVUE_CLIENT_ID, settings.FANVUE_CLIENT_SECRET = "fv-id", "fv-secret"
    conf.invalidate()
    yield
    settings.FANVUE_CLIENT_ID = settings.FANVUE_CLIENT_SECRET = ""
    conf.invalidate()


@respx.mock
def test_fanvue_sign_in_publish_metrics_and_comments(api, workspace, fanvue_app, tmp_path):
    import base64
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    from apps.channels.providers.fanvue import API, FanvueProvider

    turn_on(api, workspace)
    r = api.post("/api/channels/connect/", {"provider": "fanvue"}, format="json")
    q = parse_qs(urlparse(r.data["authorize_url"]).query)
    assert q["code_challenge_method"] == ["S256"] and "write:post" in q["scope"][0]
    token = respx.post("https://auth.fanvue.com/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    )
    respx.get(f"{API}/users/me").mock(
        return_value=httpx.Response(
            200, json={"uuid": "u-1", "handle": "jane", "displayName": "Jane", "isCreator": True}
        )
    )
    [cand] = FanvueProvider().complete_oauth(SimpleNamespace(secrets={"code_verifier": "v"}), {"code": "c"})
    req = token.calls.last.request
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"fv-id:fv-secret").decode()
    assert "client_secret" not in req.content.decode() and cand.profile_url == "https://www.fanvue.com/jane"

    from django.utils import timezone

    ch = Channel.create_for(
        workspace=workspace,
        provider="fanvue",
        external_id="u-1",
        name="Jane",
        credentials={"access_token": "at", "refresh_token": "rt"},
        config={"handle": "jane"},
        token_expires_at=timezone.now() + timezone.timedelta(hours=1),
    )
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x" * 10)
    respx.post(f"{API}/media/uploads").mock(
        return_value=httpx.Response(201, json={"mediaUuid": "m-1", "uploadId": "up1", "partSize": 6, "totalParts": 2})
    )
    respx.get(f"{API}/media/uploads/up1/parts/urls").mock(
        return_value=httpx.Response(
            200,
            json={
                "parts": [
                    {"partNumber": 1, "url": "https://s3.example/p1"},
                    {"partNumber": 2, "url": "https://s3.example/p2"},
                ]
            },
        )
    )
    put1 = respx.put("https://s3.example/p1").mock(return_value=httpx.Response(200, headers={"etag": '"e1"'}))
    respx.put("https://s3.example/p2").mock(return_value=httpx.Response(200, headers={"etag": '"e2"'}))
    complete = respx.patch(f"{API}/media/uploads/up1").mock(
        return_value=httpx.Response(200, json={"status": "processing"})
    )
    respx.get(f"{API}/media/m-1").mock(return_value=httpx.Response(200, json={"status": "ready"}))
    post = respx.post(f"{API}/posts").mock(return_value=httpx.Response(201, json={"uuid": "post-1"}))
    provider = FanvueProvider(ch)
    content = Content(
        text="New set",
        options={"audience": "followers-and-subscribers", "price": "9.99"},
        media=[MediaItem(path=p, url="u", mime="video/mp4", filename="v.mp4")],
    )
    assert provider.validate(content) == []
    res = provider.publish(content)
    assert res.external_id == "post-1" and put1.calls.last.request.content == b"x" * 6
    assert "authorization" not in put1.calls.last.request.headers
    assert json.loads(complete.calls.last.request.content) == {
        "parts": [{"PartNumber": 1, "ETag": '"e1"'}, {"PartNumber": 2, "ETag": '"e2"'}]
    }
    body = json.loads(post.calls.last.request.content)
    assert body == {"text": "New set", "audience": "followers-and-subscribers", "price": 999, "mediaUuids": ["m-1"]}
    assert post.calls.last.request.headers["x-fanvue-api-version"] == "2025-06-26"
    assert any("3.00" in x for x in provider.validate(Content(text="x", options={"price": "1"})))

    respx.get(f"{API}/posts/post-1").mock(return_value=httpx.Response(200, json={"likesCount": 12, "commentsCount": 3}))
    m = provider.fetch_metrics(["post-1"])["post-1"]
    assert (m.likes, m.comments) == (12, 3)
    respx.get(f"{API}/posts/post-1/comments").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "uuid": "c-1",
                        "text": "Love it",
                        "user": {"uuid": "fan-1", "handle": "bob", "displayName": "Bob"},
                        "createdAt": "2026-10-21T10:00:00Z",
                    },
                    {"uuid": "c-2", "text": "Thanks!", "user": {"uuid": "u-1", "handle": "jane"}},
                ]
            },
        )
    )
    entries, _ = provider.fetch_inbox({}, ["post-1"])
    assert [e.external_id for e in entries] == ["c-1"]
    reply = respx.post(f"{API}/posts/post-1/comments").mock(return_value=httpx.Response(201, json={"uuid": "c-3"}))
    provider.reply_inbox(entries[0], "Thank you")
    assert json.loads(reply.calls.last.request.content) == {"text": "@bob Thank you"}

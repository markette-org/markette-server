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

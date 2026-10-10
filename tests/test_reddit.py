from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from apps.channels.models import Channel
from apps.channels.providers import Content, MediaItem, ProviderError
from apps.channels.providers import reddit as rd
from apps.platform import conf

pytestmark = pytest.mark.django_db

CFG = {"username": "acme", "subreddit": "python", "submission_type": "any"}


def ch(workspace, config=None):
    return Channel(
        organization=workspace.organization,
        provider="reddit",
        external_id="python",
        name="r/python",
        credentials={"access_token": "tok", "refresh_token": "r"},
        config=config or dict(CFG),
    )


def form(call):
    return dict(httpx.QueryParams(call.request.content.decode()))


def submitted(name="t3_abc", url="https://www.reddit.com/r/python/comments/abc/hi/"):
    return httpx.Response(200, json={"json": {"errors": [], "data": {"name": name, "url": url, "id": "abc"}}})


@respx.mock
def test_oauth_offers_profile_and_subreddits(settings):
    settings.REDDIT_CLIENT_ID, settings.REDDIT_CLIENT_SECRET = "rid", "rsecret"
    conf.invalidate()
    token = respx.post(rd.TOKEN).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 86400})
    )
    respx.get(f"{rd.API}/api/v1/me").mock(
        return_value=httpx.Response(
            200, json={"name": "acme", "icon_img": "https://i/x.png?s=1", "subreddit": {"display_name": "u_acme"}}
        )
    )
    sr = {"display_name": "Python", "subscribers": 10, "submission_type": "self", "subreddit_type": "public"}
    respx.get(f"{rd.API}/subreddits/mine/moderator").mock(
        return_value=httpx.Response(200, json={"data": {"children": [{"data": {**sr, "display_name": "AcmeHQ"}}]}})
    )
    respx.get(f"{rd.API}/subreddits/mine/subscriber").mock(
        return_value=httpx.Response(
            200, json={"data": {"children": [{"data": sr}, {"data": {**sr, "display_name": "acmehq"}}]}}
        )
    )
    cands = rd.RedditProvider().complete_oauth(SimpleNamespace(), {"code": "c"})
    assert [c.external_id for c in cands] == ["u_acme", "acmehq", "python"]
    assert cands[0].avatar_url == "https://i/x.png" and cands[1].config["moderator"] is True
    assert cands[2].config["submission_type"] == "self" and cands[2].credentials["refresh_token"] == "r"
    req = token.calls.last.request
    assert req.headers["authorization"].startswith("Basic ") and "markette" in req.headers["user-agent"]
    assert "duration=permanent" in rd.RedditProvider().authorization_url(SimpleNamespace(state="s"))
    conf.invalidate()


@respx.mock
def test_self_post_with_flair_and_first_comment(workspace):
    c = ch(workspace)
    submit = respx.post(f"{rd.API}/api/submit").mock(return_value=submitted())
    comment = respx.post(f"{rd.API}/api/comment").mock(
        return_value=httpx.Response(
            200, json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_c"}}]}}}
        )
    )
    p = rd.RedditProvider(c)
    content = Content(text="Body", title="Hello", options={"flair_id": "f1", "nsfw": True, "first_comment": "Hi all"})
    post, first = p.prepare_first_comment(content)
    res = p.publish(post)
    assert (res.external_id, res.url) == ("t3_abc", "https://www.reddit.com/r/python/comments/abc/hi/")
    body = form(submit.calls.last)
    assert body["kind"] == "self" and body["sr"] == "python" and body["text"] == "Body"
    assert body["flair_id"] == "f1" and body["nsfw"] == "true" and body["api_type"] == "json"
    assert p.post_first_comment(res.external_id, first) == "t1_c"
    assert form(comment.calls.last) == {"api_type": "json", "thing_id": "t3_abc", "text": "Hi all"}


@respx.mock
def test_link_post_moves_text_to_first_comment(workspace):
    p = rd.RedditProvider(ch(workspace))
    submit = respx.post(f"{rd.API}/api/submit").mock(return_value=submitted())
    content = Content(
        text="Our write-up https://acme.example/post explains it.", title="T", options={"post_type": "link"}
    )
    post, first = p.prepare_first_comment(content)
    p.publish(post)
    body = form(submit.calls.last)
    assert body["kind"] == "link" and body["url"] == "https://acme.example/post" and "text" not in body
    assert first == "Our write-up  explains it."


@respx.mock
def test_image_post_uploads_and_finds_submission(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(rd.time, "sleep", lambda s: None)
    f = tmp_path / "a.png"
    f.write_bytes(b"png")
    img = MediaItem(path=Path(f), url="https://m/a.png", mime="image/png", filename="a.png", size=3)
    respx.post(f"{rd.API}/api/media/asset.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "args": {"action": "//uploads.example", "fields": [{"name": "key", "value": "k/a.png"}]},
                "asset": {"asset_id": "x"},
            },
        )
    )
    upload = respx.post("https://uploads.example").mock(return_value=httpx.Response(201))
    submit = respx.post(f"{rd.API}/api/submit").mock(
        return_value=httpx.Response(200, json={"json": {"errors": [], "data": {"websocket_url": "wss://x"}}})
    )
    listings = iter(
        [
            {"data": {"children": []}},
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "name": "t3_img",
                                "title": "Pic",
                                "subreddit": "Python",
                                "permalink": "/r/Python/comments/img/pic/",
                            }
                        }
                    ]
                }
            },
        ]
    )
    respx.get(url__regex=rf"{rd.API}/user/acme/submitted.*").mock(
        side_effect=lambda r: httpx.Response(200, json=next(listings))
    )
    res = rd.RedditProvider(ch(workspace)).publish(Content(text="", title="Pic", media=[img]))
    assert form(submit.calls.last)["url"] == "https://uploads.example/k/a.png"
    assert upload.called and res.external_id == "t3_img"
    assert res.url == "https://www.reddit.com/r/Python/comments/img/pic/"


@respx.mock
def test_errors_rate_limit_and_rules(workspace):
    p = rd.RedditProvider(ch(workspace))
    respx.post(f"{rd.API}/api/submit").mock(
        side_effect=[
            httpx.Response(200, json={"json": {"errors": [["RATELIMIT", "Take a break for 7 minutes.", "ratelimit"]]}}),
            httpx.Response(
                200,
                json={
                    "json": {
                        "errors": [["SUBMIT_VALIDATION_FLAIR_REQUIRED", "Your post must contain post flair.", "flair"]]
                    }
                },
            ),
        ]
    )
    with pytest.raises(ProviderError) as exc:
        p.publish(Content(text="x", title="t"))
    assert exc.value.retryable and exc.value.retry_after == 425
    with pytest.raises(ProviderError) as exc:
        p.publish(Content(text="x", title="t"))
    assert not exc.value.retryable and "post flair" in str(exc.value)
    only_text = rd.RedditProvider(ch(workspace, {**CFG, "submission_type": "self"}))
    problems = only_text.validate(Content(text="x", title="t" * 301, link="https://a.example"))
    assert "Reddit titles are limited to 300 characters." in problems
    assert "This subreddit only accepts text posts." in problems
    assert "A title is required." in p.validate(Content(text="x"))


@respx.mock
def test_metrics_and_flairs(workspace):
    p = rd.RedditProvider(ch(workspace))
    info = respx.get(f"{rd.API}/api/info").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "children": [
                        {
                            "data": {
                                "name": "t3_a",
                                "score": 42,
                                "num_comments": 5,
                                "num_crossposts": 1,
                                "upvote_ratio": 0.9,
                            }
                        }
                    ]
                }
            },
        )
    )
    m = p.fetch_metrics(["t3_a"])["t3_a"]
    assert (m.likes, m.comments, m.shares, m.raw["upvote_ratio"]) == (42, 5, 1, 0.9)
    assert info.calls.last.request.url.params["id"] == "t3_a"
    respx.get(f"{rd.API}/r/python/api/link_flair_v2").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "f1", "text": "News", "text_editable": False}, {"id": "f2", "text": "Mod", "mod_only": True}],
        )
    )
    assert p.flairs() == [{"id": "f1", "text": "News", "editable": False}]


@respx.mock
def test_add_specific_subreddit_reuses_account_and_loads_rules(api, workspace):
    from datetime import timedelta

    from django.utils import timezone

    profile = Channel.create_for(
        workspace=workspace,
        provider="reddit",
        external_id="u_acme",
        name="u/acme",
        credentials={"access_token": "tok", "refresh_token": "r"},
        config={"username": "acme", "subreddit": "u_acme"},
        token_expires_at=timezone.now() + timedelta(hours=5),
    )
    respx.get(f"{rd.API}/r/selfhosted/about").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "display_name": "selfhosted",
                    "subscribers": 500,
                    "submission_type": "any",
                    "subreddit_type": "public",
                }
            },
        )
    )
    respx.get(f"{rd.API}/api/v1/selfhosted/post_requirements").mock(
        return_value=httpx.Response(
            200, json={"is_flair_required": True, "title_required_strings": ["[Release]"], "body_text_max_length": None}
        )
    )
    respx.get(f"{rd.API}/r/selfhosted/about/rules").mock(
        return_value=httpx.Response(200, json={"rules": [{"short_name": "No spam", "kind": "all"}]})
    )
    r = api.post(
        f"/api/channels/{profile.id}/add-subreddit/", {"name": "https://www.reddit.com/r/selfhosted/"}, format="json"
    )
    assert r.status_code == 201, r.content
    sub = Channel.objects.get(provider="reddit", external_id="selfhosted")
    assert sub.credentials["refresh_token"] == "r" and sub.followers_count == 500
    assert sub.config["requirements"] == {"is_flair_required": True, "title_required_strings": ["[Release]"]}
    assert sub.config["rules"] == ["No spam"]
    problems = rd.RedditProvider(sub).validate(Content(text="x", title="New version"))
    assert "r/selfhosted requires a post flair." in problems
    assert "r/selfhosted titles must contain one of: [Release]." in problems
    ok = Content(text="x", title="[Release] v2", options={"flair_id": "f"})
    assert rd.RedditProvider(sub).validate(ok) == []

    respx.get(f"{rd.API}/r/nope/about").mock(return_value=httpx.Response(404, json={}))
    r = api.post(f"/api/channels/{profile.id}/add-subreddit/", {"name": "r/nope"}, format="json")
    assert r.status_code == 400 and "does not exist" in r.json()["detail"]
    assert (
        api.post(f"/api/channels/{profile.id}/add-subreddit/", {"name": "bad name!"}, format="json").status_code == 400
    )


def test_link_domain_rules(workspace):
    cfg = {**CFG, "requirements": {"link_restriction_policy": "blacklist", "domain_blacklist": ["medium.com"]}}
    p = rd.RedditProvider(ch(workspace, cfg))
    content = Content(text="", title="t", link="https://blog.medium.com/x", options={"post_type": "link"})
    assert p.validate(content) == ["r/python does not accept links to blog.medium.com."]


@respx.mock
def test_revoked_token_flags_channel_for_reauth(api, workspace):
    from datetime import timedelta

    from django.utils import timezone

    c = Channel.create_for(
        workspace=workspace,
        provider="reddit",
        external_id="python",
        name="r/python",
        credentials={"access_token": "tok", "refresh_token": "r"},
        config=dict(CFG),
        token_expires_at=timezone.now() + timedelta(hours=5),
    )
    respx.get(f"{rd.API}/r/python/api/link_flair_v2").mock(return_value=httpx.Response(401, json={"error": 401}))
    r = api.get(f"/api/channels/{c.id}/flairs/")
    assert r.status_code == 400 and "reconnect Reddit" in r.json()["detail"]
    c.refresh_from_db()
    assert c.status == "needs_reauth"

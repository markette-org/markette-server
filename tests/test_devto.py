import json
from pathlib import Path

import httpx
import pytest
import respx

from apps.channels.models import Channel
from apps.channels.providers import Content, MediaItem, ProviderError
from apps.channels.providers.devto import DEFAULT_BASE, DevtoProvider, normalize_tags

pytestmark = pytest.mark.django_db

ME = {"id": 7, "username": "ada", "name": "Ada", "profile_image": "https://img/ada.png"}


def media(tmp_path, name="a.png"):
    p = tmp_path / name
    p.write_bytes(b"img")
    return MediaItem(path=Path(p), url=f"https://markette.example/media/{name}", mime="image/png", filename=name)


def devto_channel(workspace, **config):
    return Channel(
        organization=workspace.organization,
        provider="devto",
        external_id="user:7",
        name="Ada",
        credentials={"api_key": "k"},
        config={"default_status": "publish", "username": "ada", **config},
    )


def test_normalize_tags():
    assert normalize_tags(["Python", "web-dev", "C++", "python", "AI", "extra"]) == ["python", "webdev", "c", "ai"]


@respx.mock
def test_connect_user_and_organization():
    me = respx.get(f"{DEFAULT_BASE}/api/users/me").mock(return_value=httpx.Response(200, json=ME))
    [c] = DevtoProvider().connect_with_credentials({"api_key": " k ", "default_status": "draft"})
    assert (c.external_id, c.handle, c.profile_url) == ("user:7", "@ada", "https://dev.to/ada")
    assert c.credentials == {"api_key": "k"} and c.config["default_status"] == "draft" and c.base_url == ""
    assert me.calls.last.request.headers["api-key"] == "k"
    assert me.calls.last.request.headers["accept"] == "application/vnd.forem.api-v1+json"
    respx.get(f"{DEFAULT_BASE}/api/organizations/acme").mock(
        return_value=httpx.Response(200, json={"id": 3, "username": "acme", "name": "Acme", "profile_image": ""})
    )
    [org] = DevtoProvider().connect_with_credentials({"api_key": "k", "organization": "https://dev.to/acme"})
    assert org.external_id == "org:3" and org.config["organization_id"] == 3 and org.name == "Acme"


@respx.mock
def test_connect_other_forem_and_bad_key():
    respx.get("https://forem.example/api/users/me").mock(return_value=httpx.Response(200, json=ME))
    [c] = DevtoProvider().connect_with_credentials({"api_key": "k", "base_url": "https://forem.example/"})
    assert c.base_url == "https://forem.example" and c.profile_url == "https://forem.example/ada"
    respx.get(f"{DEFAULT_BASE}/api/users/me").mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(ProviderError, match="rejected this API key"):
        DevtoProvider().connect_with_credentials({"api_key": "bad"})
    with pytest.raises(ProviderError, match="community's address"):
        DevtoProvider().connect_with_credentials({"api_key": "k", "base_url": "https://forem.example/path"})


@respx.mock
def test_publish_article(workspace, tmp_path):
    post = respx.post(f"{DEFAULT_BASE}/api/articles").mock(
        return_value=httpx.Response(201, json={"id": 99, "url": "https://dev.to/ada/hello-1abc"})
    )
    cover, second = media(tmp_path), media(tmp_path, "b.png")
    second.alt = "Diagram"
    res = DevtoProvider(devto_channel(workspace, organization_id=3)).publish(
        Content(
            text="Hello **world**",
            title="Hello",
            link="https://acme.org/hello",
            media=[cover, second],
            tags=["Python", "Web Dev"],
            options={"excerpt": "Short", "series": "Basics"},
        )
    )
    assert (res.external_id, res.url) == ("99", "https://dev.to/ada/hello-1abc")
    article = json.loads(post.calls.last.request.content)["article"]
    assert article == {
        "title": "Hello",
        "body_markdown": "Hello **world**\n\n![Diagram](https://markette.example/media/b.png)",
        "published": True,
        "tags": ["python", "webdev"],
        "main_image": "https://markette.example/media/a.png",
        "canonical_url": "https://acme.org/hello",
        "description": "Short",
        "series": "Basics",
        "organization_id": 3,
    }


@respx.mock
def test_draft_option_and_validation(workspace):
    post = respx.post(f"{DEFAULT_BASE}/api/articles").mock(return_value=httpx.Response(201, json={"id": 5, "url": ""}))
    provider = DevtoProvider(devto_channel(workspace))
    provider.publish(Content(text="x", title="T", options={"status": "draft"}))
    assert json.loads(post.calls.last.request.content)["article"]["published"] is False
    assert "title is required" in " ".join(provider.validate(Content(text="x")))
    assert any("128" in p for p in provider.validate(Content(text="x", title="t" * 129)))


@respx.mock
def test_delete_unpublishes(workspace):
    put = respx.put(f"{DEFAULT_BASE}/api/articles/99").mock(return_value=httpx.Response(200, json={"id": 99}))
    DevtoProvider(devto_channel(workspace)).delete("99")
    assert json.loads(put.calls.last.request.content) == {"article": {"published": False}}


@respx.mock
def test_metrics_and_followers(workspace):
    respx.get(f"{DEFAULT_BASE}/api/articles/me/all").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 99, "page_views_count": 120, "public_reactions_count": 9, "comments_count": 2},
                {"id": 98, "page_views_count": 1, "public_reactions_count": 0, "comments_count": 0},
            ],
        )
    )
    provider = DevtoProvider(devto_channel(workspace))
    m = provider.fetch_metrics(["99"])["99"]
    assert (m.impressions, m.likes, m.comments) == (120, 9, 2)
    respx.get(f"{DEFAULT_BASE}/api/followers/users").mock(return_value=httpx.Response(200, json=[{}] * 3))
    assert provider.fetch_audience() == 3
    assert DevtoProvider(devto_channel(workspace, organization_id=3)).fetch_audience() is None


@respx.mock
def test_rate_limit_and_auth_errors(workspace):
    route = respx.post(f"{DEFAULT_BASE}/api/articles")
    route.mock(return_value=httpx.Response(429, headers={"retry-after": "30"}, json={"error": "rate limit"}))
    provider = DevtoProvider(devto_channel(workspace))
    with pytest.raises(ProviderError) as exc:
        provider.publish(Content(text="x", title="T"))
    assert exc.value.retryable and exc.value.retry_after == 30
    route.mock(return_value=httpx.Response(403, json={"error": "forbidden"}))
    with pytest.raises(ProviderError) as exc:
        provider.publish(Content(text="x", title="T"))
    assert exc.value.auth

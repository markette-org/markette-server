import httpx
import pytest
import respx

from apps.channels.models import Channel
from apps.channels.providers import Content, ProviderError
from apps.channels.providers.blogs import WordPressProvider
from apps.channels.providers.mastodon import MastodonProvider


@respx.mock
def test_mastodon_publish_sends_idempotency_key(workspace):
    ch = Channel(
        organization=workspace.organization,
        provider="mastodon",
        external_id="1",
        name="m",
        base_url="https://mas.to",
        credentials={"access_token": "tok"},
    )
    route = respx.post("https://mas.to/api/v1/statuses").mock(
        return_value=httpx.Response(200, json={"id": "42", "url": "https://mas.to/@me/42"})
    )
    res = MastodonProvider(ch).publish(Content(text="hi", idempotency_key="markette-target-9"))
    assert res.external_id == "42"
    req = route.calls.last.request
    assert req.headers["Idempotency-Key"] == "markette-target-9"
    assert req.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_rate_limit_is_retryable(workspace):
    ch = Channel(
        organization=workspace.organization,
        provider="mastodon",
        external_id="1",
        name="m",
        base_url="https://mas.to",
        credentials={"access_token": "tok"},
    )
    respx.post("https://mas.to/api/v1/statuses").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "90"}, json={"error": "slow down"})
    )
    with pytest.raises(ProviderError) as exc:
        MastodonProvider(ch).publish(Content(text="hi"))
    assert exc.value.retryable and exc.value.retry_after == 90


@respx.mock
def test_wordpress_publish_renders_markdown_and_creates_tags(workspace):
    ch = Channel(
        organization=workspace.organization,
        provider="wordpress",
        external_id="x",
        name="wp",
        base_url="https://blog.example",
        credentials={"username": "u", "app_password": "p"},
        config={"default_status": "publish"},
    )
    respx.get("https://blog.example/wp-json/wp/v2/tags").mock(return_value=httpx.Response(200, json=[]))
    respx.post("https://blog.example/wp-json/wp/v2/tags").mock(
        return_value=httpx.Response(201, json={"id": 5, "name": "launch"})
    )
    posts = respx.post("https://blog.example/wp-json/wp/v2/posts").mock(
        return_value=httpx.Response(201, json={"id": 77, "link": "https://blog.example/?p=77"})
    )
    res = WordPressProvider(ch).publish(Content(text="Hello *world*", title="T", tags=["launch"]))
    assert res.external_id == "77"
    body = posts.calls.last.request.content.decode()
    assert "<em>world</em>" in body and '"tags": [5]' in body.replace(":[", ": [")

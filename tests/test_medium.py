import json
from pathlib import Path

import httpx
import respx

from apps.channels.models import Channel
from apps.channels.providers import Content, MediaItem
from apps.channels.providers.medium import API, MediumProvider


@respx.mock
def test_connect_lists_account_and_publications():
    respx.get(f"{API}/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": "u1",
                    "username": "ada",
                    "name": "Ada",
                    "url": "https://medium.com/@ada",
                    "imageUrl": "https://img/a",
                }
            },
        )
    )
    respx.get(f"{API}/users/u1/publications").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "p9", "name": "Acme Engineering", "url": "https://medium.com/acme", "imageUrl": ""}]},
        )
    )
    cands = MediumProvider().connect_with_credentials({"integration_token": " tok ", "default_status": "draft"})
    assert [c.external_id for c in cands] == ["user:u1", "publication:p9"]
    assert cands[1].config == {
        "default_status": "draft",
        "notify_followers": True,
        "author_id": "u1",
        "publication_id": "p9",
        "target": "publication",
    }
    assert cands[0].credentials == {"integration_token": "tok"}


@respx.mock
def test_publish_to_publication_with_image_and_canonical(workspace, tmp_path):
    ch = Channel(
        organization=workspace.organization,
        provider="medium",
        external_id="publication:p9",
        name="Acme",
        credentials={"integration_token": "tok"},
        config={
            "author_id": "u1",
            "publication_id": "p9",
            "target": "publication",
            "default_status": "public",
            "notify_followers": False,
        },
    )
    respx.post(f"{API}/images").mock(
        return_value=httpx.Response(201, json={"data": {"url": "https://cdn-medium/x.png"}})
    )
    route = respx.post(f"{API}/publications/p9/posts").mock(
        return_value=httpx.Response(201, json={"data": {"id": "abc123", "url": "https://medium.com/acme/hello-abc123"}})
    )
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    media = [MediaItem(path=Path(img), url="https://markette/media/x.png", mime="image/png", alt="chart")]
    content = Content(
        text="Intro\n\n![chart](https://markette/media/x.png)",
        title="Hello",
        link="https://blog.example/hello",
        tags=["a", "b", "c", "d"],
        media=media,
    )
    assert MediumProvider(ch).validate(content) == ["Medium accepts at most 3 tags."]
    res = MediumProvider(ch).publish(content)
    assert res.external_id == "abc123"
    body = json.loads(route.calls.last.request.content)
    assert body["content"] == "# Hello\n\nIntro\n\n![chart](https://cdn-medium/x.png)"
    assert body["tags"] == ["a", "b", "c"]
    assert body["canonicalUrl"] == "https://blog.example/hello"
    assert (body["publishStatus"], body["notifyFollowers"]) == ("public", False)

import json

import httpx
import pytest
import respx

from apps.channels.models import Channel
from apps.channels.providers import Content
from apps.channels.providers.linkedin import REST, LinkedInProvider
from apps.publishing.models import PostTarget, TargetStatus

pytestmark = pytest.mark.django_db(transaction=True)


def li_channel(workspace, **config):
    return Channel.create_for(
        workspace=workspace,
        provider="linkedin",
        external_id="urn:li:person:abc",
        name="Ada on LinkedIn",
        credentials={"access_token": "tok", "author": "urn:li:person:abc"},
        config=config,
    )


def test_links_move_to_first_comment(workspace):
    ch = li_channel(workspace, links_in_first_comment=True)
    post, comment = LinkedInProvider(ch).prepare_first_comment(
        Content(
            text="Our report is out: https://acme.example/report.\n\nRead it now",
            link="https://acme.example/report",
            options={"first_comment": "Full report 👇"},
        )
    )
    assert post.text == "Our report is out:\n\nRead it now" and post.link == ""
    assert comment == "Full report 👇\nhttps://acme.example/report"
    # The target option overrides the channel default.
    post, comment = LinkedInProvider(ch).prepare_first_comment(
        Content(text="See https://x.example", options={"links_in_first_comment": False})
    )
    assert post.text == "See https://x.example" and comment == ""


@respx.mock
def test_publish_posts_then_comments(api, workspace):
    ch = li_channel(workspace)
    respx.post(f"{REST}/posts").mock(return_value=httpx.Response(201, headers={"x-restli-id": "urn:li:share:9"}))
    comments = respx.post(f"{REST}/socialActions/urn%3Ali%3Ashare%3A9/comments").mock(
        return_value=httpx.Response(201, json={"id": "c1"})
    )
    r = api.post(
        "/api/posts/",
        {
            "body": "Big news https://acme.example/n",
            "targets": [{"channel": ch.id, "options": {"links_in_first_comment": True, "first_comment": "Details:"}}],
        },
        format="json",
    )
    api.post(f"/api/posts/{r.data['id']}/schedule/", {"mode": "now"}, format="json")
    t = PostTarget.objects.get(post_id=r.data["id"])
    assert t.status == TargetStatus.PUBLISHED and t.extra["first_comment_id"] == "c1" and t.last_error == ""
    posted = json.loads(respx.calls[0].request.content)
    assert posted["commentary"] == "Big news" and "content" not in posted
    sent = json.loads(comments.calls.last.request.content)
    assert sent == {
        "actor": "urn:li:person:abc",
        "object": "urn:li:share:9",
        "message": {"text": "Details:\nhttps://acme.example/n"},
    }


@respx.mock
def test_comment_failure_keeps_the_post_published(api, workspace):
    ch = li_channel(workspace, links_in_first_comment=True)
    respx.post(f"{REST}/posts").mock(return_value=httpx.Response(201, headers={"x-restli-id": "urn:li:share:10"}))
    respx.post(url__regex=r".*/socialActions/.*/comments").mock(
        return_value=httpx.Response(403, json={"message": "no"})
    )
    r = api.post("/api/posts/", {"body": "Hi https://a.example", "targets": [{"channel": ch.id}]}, format="json")
    api.post(f"/api/posts/{r.data['id']}/schedule/", {"mode": "now"}, format="json")
    t = PostTarget.objects.get(post_id=r.data["id"])
    assert t.status == TargetStatus.PUBLISHED and "first comment could not be posted" in t.last_error
    assert len([c for c in respx.calls if c.request.url.path.endswith("/posts")]) == 1


def test_validation_uses_text_without_links_and_limits_comment(workspace):
    ch = li_channel(workspace, links_in_first_comment=True)
    long_url = "https://acme.example/" + "a" * 200
    ok = LinkedInProvider(ch).validate(Content(text="x" * 2990 + " " + long_url))
    assert ok == []
    bad = LinkedInProvider(ch).validate(Content(text="hi", options={"first_comment": "y" * 1300}))
    assert bad == ["The first comment exceeds 1250 characters."]

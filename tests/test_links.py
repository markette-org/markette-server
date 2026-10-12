from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client

from apps.links.models import LinkClickDay, ShortLink
from apps.links.tracking import add_utm
from apps.publishing.models import Campaign, PostTarget

pytestmark = pytest.mark.django_db(transaction=True)


def publish(api, channel, body, **extra):
    post = api.post("/api/posts/", {"body": body, "targets": [{"channel": channel.id}], **extra}, format="json").data
    r = api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    assert r.status_code == 200, r.data
    return post


def track(api, workspace, **cfg):
    r = api.patch(f"/api/workspaces/{workspace.id}/", {"link_tracking": cfg}, format="json")
    assert r.status_code == 200, r.data
    return r.data["link_tracking"]


def test_add_utm_keeps_existing_parameters():
    url = add_utm("https://acme.org/a?utm_source=news&x=1", source="x", medium="social", campaign="c", content="p")
    q = parse_qs(urlsplit(url).query)
    assert q == {
        "utm_source": ["news"],
        "x": ["1"],
        "utm_medium": ["social"],
        "utm_campaign": ["c"],
        "utm_content": ["p"],
    }


def test_utm_tags_only_own_links(api, workspace, channel, fake_provider):
    assert track(api, workspace, utm=True, domains=["https://www.Acme.org/blog"])["domains"] == ["acme.org"]
    today = date.today()
    campaign = Campaign.objects.create(workspace=workspace, name="Spring Launch", starts_on=today, ends_on=today)
    post = publish(
        api,
        channel,
        "Read https://blog.acme.org/hello, not https://other.example/x.",
        campaign=campaign.id,
    )
    text = fake_provider.published[-1][1].text
    tagged = text.split(" ")[1].rstrip(",")
    assert tagged.startswith("https://blog.acme.org/hello?utm_source=fake&utm_medium=social")
    assert f"utm_campaign=spring-launch&utm_content=post-{post['id']}" in tagged
    assert "https://other.example/x." in text  # not ours, and the full stop stays outside the link


def test_short_links_count_human_clicks(api, workspace, channel, fake_provider):
    track(api, workspace, utm=True, short_links=True)  # no domains: the post's own link
    publish(api, channel, "New article https://acme.org/post", link="https://acme.org/post")
    text = fake_provider.published[-1][1].text
    short = text.split()[-1]
    link = ShortLink.objects.get()
    assert short.endswith(f"/l/{link.code}") and "utm_source=fake" in link.url
    c = Client()
    r = c.get(f"/l/{link.code}", HTTP_USER_AGENT="Mozilla/5.0 (Macintosh)")
    assert r.status_code == 302 and r["Location"] == link.url
    c.get(f"/l/{link.code}", HTTP_USER_AGENT="Twitterbot/1.0")  # a link preview, not a reader
    c.get(f"/l/{link.code}", HTTP_USER_AGENT="Mozilla/5.0 (iPhone)")
    target = PostTarget.objects.get()
    assert target.link_clicks == 2 and LinkClickDay.objects.get().clicks == 2
    assert api.get(f"/api/posts/{target.post_id}/").data["targets"][0]["link_clicks"] == 2
    assert api.get("/api/analytics/overview/").data["totals"]["link_clicks"] == 2
    assert c.get("/l/nope").status_code == 404


def test_retries_reuse_the_same_short_link(api, workspace, channel, fake_provider):
    from apps.publishing.tasks import build_content

    track(api, workspace, short_links=True)
    post = api.post(
        "/api/posts/",
        {"body": "https://acme.org/a", "link": "https://acme.org/a", "targets": [{"channel": channel.id}]},
        format="json",
    ).data
    target = PostTarget.objects.get(post_id=post["id"])
    assert build_content(target).text == build_content(target).text and ShortLink.objects.count() == 1


def test_off_by_default_and_never_on_blogs(api, workspace, channel, fake_provider, monkeypatch):
    publish(api, channel, "https://acme.org/a", link="https://acme.org/a")
    assert fake_provider.published[-1][1].text == "https://acme.org/a"
    track(api, workspace, utm=True)
    monkeypatch.setattr(fake_provider.capabilities, "kind", "blog")
    publish(api, channel, "https://acme.org/b", link="https://acme.org/b")
    assert fake_provider.published[-1][1].link == "https://acme.org/b"

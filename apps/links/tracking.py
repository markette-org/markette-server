"""UTM tags and short links for the links in social posts.

A workspace turns them on in Settings → Workspace (``Workspace.link_tracking``). Only links
to the workspace's own sites are touched: the domains listed there, or, when none are listed,
the post's link (and the address of a publication an announcement points to). Blogs and
newsletters are left alone: their canonical links must stay clean.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from .models import ShortLink

DEFAULTS = {"utm": False, "utm_medium": "social", "short_links": False, "domains": []}
URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
TRAILING = ".,;:!?)]}»”’"


def config(workspace):
    return {**DEFAULTS, **(workspace.link_tracking or {})}


def short_url(code):
    return f"{settings.SHORT_LINK_BASE}/l/{code}"


def _host(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _tracked(url, domains, own):
    host = _host(url)
    if domains:
        return any(host == d or host.endswith("." + d) for d in domains)
    return url in own


def add_utm(url, *, source, medium, campaign, content):
    """Adds utm_* parameters the URL does not already have (yours win)."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    have = {k for k, _ in query}
    extra = [
        (k, v)
        for k, v in (
            ("utm_source", source),
            ("utm_medium", medium),
            ("utm_campaign", campaign),
            ("utm_content", content),
        )
        if v and k not in have
    ]
    return urlunsplit(parts._replace(query=urlencode(query + extra)))


def _short(target, url):
    link = ShortLink.objects.filter(target=target, url=url).first()
    if link:
        return link
    try:
        with transaction.atomic():
            return ShortLink.objects.create(workspace=target.post.workspace, target=target, url=url)
    except IntegrityError:  # created concurrently
        return ShortLink.objects.get(target=target, url=url)


def apply(target, text, link, *, provider_key, kind):
    """(text, link) with the workspace's tracking applied to ``target``'s links."""
    cfg = config(target.post.workspace)
    if kind != "social" or not (cfg["utm"] or cfg["short_links"]) or (target.options or {}).get("no_tracking"):
        return text, link
    post = target.post
    domains = [d.strip().lower().removeprefix("www.") for d in cfg["domains"] if d.strip()]
    own = {u for u in (post.link, link) if u}
    campaign = slugify(post.campaign.name) if post.campaign_id else "markette"
    cache = {}

    def convert(url):
        if url in cache:
            return cache[url]
        if url.startswith(settings.SHORT_LINK_BASE + "/l/") or not _tracked(url, domains, own):
            cache[url] = url
            return url
        out = url
        if cfg["utm"]:
            out = add_utm(
                out,
                source=provider_key,
                medium=cfg["utm_medium"] or "social",
                campaign=campaign,
                content=f"post-{post.pk}",
            )
        if cfg["short_links"]:
            out = short_url(_short(target, out).code)
        cache[url] = out
        return out

    def sub(match):
        url = match.group(0)
        tail = ""
        while url and url[-1] in TRAILING:
            url, tail = url[:-1], url[-1] + tail
        return convert(url) + tail

    return URL_RE.sub(sub, text), (convert(link) if link else link)

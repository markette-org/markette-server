import re

from django.db.models import F
from django.http import HttpResponseNotFound, HttpResponseRedirect
from django.utils import timezone

from apps.publishing.models import PostTarget

from .models import LinkClickDay, ShortLink

# Link previews and crawlers fetch links too; they are not readers.
BOTS = re.compile(
    r"bot|crawl|spider|slurp|preview|facebookexternalhit|embedly|cardyb|mastodon|pleroma|misskey|"
    r"whatsapp|telegram|discord|slack|skype|linkedin|vkshare|quora|pinterest|bitly|curl|wget|python|go-http|"
    r"okhttp|headless|lighthouse|monitor",
    re.I,
)


def follow(request, code):
    link = ShortLink.objects.filter(code=code).only("id", "url", "target_id").first()
    if link is None:
        return HttpResponseNotFound("Unknown link.")
    agent = request.headers.get("User-Agent", "")
    if agent and not BOTS.search(agent) and request.method == "GET":
        now = timezone.now()
        ShortLink.objects.filter(pk=link.pk).update(clicks=F("clicks") + 1, last_clicked_at=now)
        day, _ = LinkClickDay.objects.get_or_create(link_id=link.pk, day=now.date())
        LinkClickDay.objects.filter(pk=day.pk).update(clicks=F("clicks") + 1)
        if link.target_id:
            PostTarget.objects.filter(pk=link.target_id).update(link_clicks=F("link_clicks") + 1)
    response = HttpResponseRedirect(link.url)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "unsafe-url"
    return response

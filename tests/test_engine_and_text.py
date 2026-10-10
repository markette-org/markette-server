from datetime import timedelta

from django.utils import timezone

from apps.analytics.models import MetricSnapshot
from apps.channels.providers.bluesky import BlueskyProvider
from apps.channels.providers.text import graphemes, measure
from apps.insights import engine
from apps.publishing.models import Post, PostTarget, TargetStatus


def test_x_counts_urls_as_23():
    assert measure("see https://example.com/a/very/long/path/that/goes/on", "x") == 4 + 23
    assert measure("héllo", "x") == 5
    assert measure("日本", "x") == 4


def test_graphemes_counts_emoji_sequences_once():
    assert graphemes("👍🏽") == 1
    assert graphemes("👩‍👩‍👧") == 1
    assert graphemes("é") == 1


def test_bluesky_facets_use_utf8_byte_offsets():
    text = "Café #news https://ex.am/ple"
    facets = BlueskyProvider().facets(text)
    by_type = {f["features"][0]["$type"].split("#")[1]: f["index"] for f in facets}
    raw = text.encode()
    assert raw[by_type["tag"]["byteStart"] : by_type["tag"]["byteEnd"]] == b"#news"
    assert raw[by_type["link"]["byteStart"] : by_type["link"]["byteEnd"]] == b"https://ex.am/ple"


def test_prior_profile_prefers_working_hours_on_linkedin(channel):
    channel.provider = "linkedin"
    scores, n, _ = engine.compute_grid(channel)
    assert n == 0
    assert scores[1][9] > scores[1][3]  # Tuesday 9:00 beats 3:00
    assert scores[2][10] > scores[6][10]  # Wednesday beats Sunday


def test_history_moves_the_peak(channel, workspace):
    tz = workspace.tz
    now = timezone.now()
    # Saturday 21:00 posts do great, Thursday noon posts (a prior favourite) do poorly.
    for weekday, hour, rate in ((5, 21, 0.25), (3, 12, 0.02)):
        for i in range(10):
            local = (now - timedelta(days=7 * (i + 1))).astimezone(tz)
            local = local - timedelta(days=(local.weekday() - weekday) % 7)
            when = local.replace(hour=hour, minute=5)
            post = Post.objects.create(workspace=workspace, body=f"p{weekday}{i}")
            t = PostTarget.objects.create(
                post=post,
                channel=channel,
                status=TargetStatus.PUBLISHED,
                published_at=when,
                external_id=f"e{weekday}{i}",
            )
            MetricSnapshot.objects.create(
                target=t,
                captured_at=when + timedelta(hours=24),
                age_hours=24,
                engagement_rate=rate,
                engagements=int(rate * 100),
                impressions=100,
            )
    scores, n, counts = engine.compute_grid(channel)
    assert n == 20 and counts[5][21] == 10
    assert scores[3][12] < 20
    assert engine.top_slots(scores, 1)[0] == {"weekday": 5, "hour": 21, "score": 100.0}


def test_next_best_slot_respects_taken(channel):
    start = timezone.now()
    first = engine.next_best_slot(channel, start, start + timedelta(days=7), [], timedelta(hours=2))
    second = engine.next_best_slot(channel, start, start + timedelta(days=7), [first], timedelta(hours=2))
    assert first and second and abs((second - first).total_seconds()) >= 7200

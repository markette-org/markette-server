"""AI-assisted scheduling and copywriting through any OpenAI-compatible API.

Two providers are supported (Platform settings → AI):

* ``openrouter``: OpenRouter (https://openrouter.ai), one key for hundreds of models;
  token cost is reported by OpenRouter itself.
* ``openai_compatible``: any server speaking the OpenAI Chat Completions API
  (OpenAI, Azure OpenAI, vLLM, Ollama, LM Studio, LiteLLM…) at ``AI_BASE_URL``.

The statistical engine (engine.py) always does the number crunching; the model
is given its output plus context the numbers cannot see — what the post is
about, deadlines mentioned in it, the campaign period, what else is already on
the calendar — and asked to choose and explain. Every time it proposes is
validated against the window and the calendar before it is returned.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

import openai
from django.conf import settings
from django.utils import timezone
from pydantic import BaseModel, Field, ValidationError

from apps.channels.providers import get_provider
from apps.platform import conf
from apps.publishing.models import PostTarget, TargetStatus

from . import engine
from .models import AIRequest

log = logging.getLogger(__name__)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OPENROUTER_URL = "https://openrouter.ai/api/v1"


class AIUnavailable(Exception):
    pass


# --- structured outputs ------------------------------------------------------


class SlotChoice(BaseModel):
    channel_id: int
    local_time: str = Field(description="Local wall-clock time in the workspace time zone, format YYYY-MM-DDTHH:MM")
    rationale: str = Field(description="One or two sentences a social media manager would find useful")


class TimeSuggestions(BaseModel):
    choices: list[SlotChoice]
    summary: str


class PlannedPublication(BaseModel):
    channel_id: int
    local_time: str = Field(description="YYYY-MM-DDTHH:MM in the workspace time zone")
    body: str
    title: str = Field(default="", description="Only for blog channels")


class PlannedPost(BaseModel):
    angle: str = Field(description="The idea of this post in a few words")
    publications: list[PlannedPublication]


class CampaignPlan(BaseModel):
    strategy: str = Field(description="Short explanation of the cadence and why")
    posts: list[PlannedPost]


class Variant(BaseModel):
    channel_id: int
    body: str
    title: str = ""
    notes: str = ""


class Adaptation(BaseModel):
    variants: list[Variant]


# --- plumbing ----------------------------------------------------------------


def base_url():
    url = (conf.get("AI_BASE_URL") or "").strip().rstrip("/")
    if conf.get("AI_PROVIDER") == "openrouter":
        return url or OPENROUTER_URL
    return url


def make_client(api_key=None, timeout=120.0, max_retries=3):
    """An OpenAI SDK client pointed at the configured provider."""
    url = base_url()
    if not url:
        raise AIUnavailable("Set the base URL of your OpenAI-compatible API in Platform settings.")
    headers = {}
    if conf.get("AI_PROVIDER") == "openrouter":
        # OpenRouter attribution headers (optional, shown in their dashboard).
        from apps.platform.models import PlatformSettings

        headers = {"HTTP-Referer": settings.FRONTEND_URL, "X-Title": PlatformSettings.load().instance_name}
    return openai.OpenAI(
        api_key=api_key or conf.get("AI_API_KEY") or "not-needed",  # local servers often take no key
        base_url=url,
        default_headers=headers,
        max_retries=max_retries,
        timeout=timeout,
    )


def _client():
    if not conf.get("AI_ENABLED"):
        raise AIUnavailable("AI features are not configured (set an AI provider and key in Platform settings).")
    if not conf.get("AI_MODEL"):
        raise AIUnavailable("No AI model is configured.")
    return make_client()


SYSTEM = (
    "You are the scheduling and copy assistant inside Markette, a tool that publishes the same content "
    "across social networks (X, Bluesky, Mastodon, LinkedIn, Facebook, Instagram) and sites (WordPress, "
    "Drupal, Medium, Hugo). You work for a social media manager. Ground every timing decision in the data you "
    "are given: per-channel best-time scores computed from that channel's own past engagement (or platform "
    "priors when there is little history), the calendar of already scheduled posts, and the content itself. "
    "Respect hard constraints: stay inside the requested window, keep at least the minimum gap between posts "
    "on the same channel, and publish before any deadline or event the content mentions. Write rationales "
    "for a busy human: specific, short, no filler. Always answer with a single JSON object and nothing else."
)
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _response_format(mode, kind, schema):
    if mode == "json_schema":
        return {"type": "json_schema", "json_schema": {"name": kind, "schema": schema.model_json_schema()}}
    if mode == "json_object":
        return {"type": "json_object"}
    return None  # "prompt": rely on the instructions alone


def _parse(content, schema):
    text = FENCE_RE.sub("", (content or "").strip())
    if not text.startswith("{"):
        # Some models wrap JSON in prose; take the outermost object.
        first, last = text.find("{"), text.rfind("}")
        text = text[first : last + 1] if first != -1 and last > first else text
    return schema.model_validate(json.loads(text))


def _call(kind, workspace, user, prompt, schema, max_tokens=16000):
    from apps.tenants.limits import ai_quota_left

    left = ai_quota_left(workspace.organization)
    if left is not None and left <= 0:
        raise AIUnavailable("Your organization has used this month's AI allowance.")
    client = _client()
    provider = conf.get("AI_PROVIDER")
    model = conf.get("AI_MODEL")
    effort = conf.get("AI_EFFORT")
    content = (
        f"{prompt}\n\nAnswer with one JSON object that validates against this JSON Schema:\n"
        f"{json.dumps(schema.model_json_schema())}"
    )
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
    extra_body = {}
    if provider == "openrouter":
        extra_body["usage"] = {"include": True}  # OpenRouter returns the real cost
        if effort:
            extra_body["reasoning"] = {"effort": effort}
    elif effort:
        extra_body["reasoning_effort"] = effort
    modes = [conf.get("AI_STRUCTURED_OUTPUT") or "json_schema"]
    if modes[0] == "json_schema":
        modes.append("json_object")  # servers without schema support
    started = timezone.now()
    resp = None
    for i, mode in enumerate(modes):
        kwargs = {"model": model, "messages": messages, "max_tokens": max_tokens}
        fmt = _response_format(mode, kind, schema)
        if fmt:
            kwargs["response_format"] = fmt
        try:
            resp = client.chat.completions.create(**kwargs, extra_body=extra_body or None)
            break
        except openai.BadRequestError as exc:
            message = str(exc).lower()
            if i + 1 < len(modes) and ("response_format" in message or "json_schema" in message):
                log.info("Provider rejected %s output, retrying with %s", mode, modes[i + 1])
                continue
            log.warning("AI request rejected: %s", exc)
            raise AIUnavailable(f"The AI provider rejected the request: {_short_error(exc)}") from exc
        except openai.AuthenticationError as exc:
            raise AIUnavailable("The AI provider rejected the API key.") from exc
        except openai.NotFoundError as exc:
            raise AIUnavailable(f"Model {model} was not found at the AI provider.") from exc
        except openai.RateLimitError as exc:
            raise AIUnavailable("The AI service is rate limited; try again in a minute.") from exc
        except openai.APIStatusError as exc:
            log.warning("AI API error %s: %s", exc.status_code, exc)
            raise AIUnavailable(f"AI request failed ({exc.status_code}).") from exc
        except openai.APIConnectionError as exc:
            raise AIUnavailable("Could not reach the AI service.") from exc
    choice = resp.choices[0] if resp.choices else None
    finish = (choice.finish_reason if choice else "") or ""
    parsed, error = None, None
    if choice and choice.message and choice.message.content:
        try:
            parsed = _parse(choice.message.content, schema)
        except (ValueError, ValidationError) as exc:
            error = exc
    usage = resp.usage
    cost = None
    if usage is not None and getattr(usage, "model_extra", None):
        cost = usage.model_extra.get("cost")
    AIRequest.objects.create(
        workspace=workspace,
        user=user,
        kind=kind,
        model=resp.model or model,
        prompt=prompt[:20000],
        response=parsed.model_dump() if parsed else {"raw": (choice.message.content or "")[:20000] if choice else ""},
        input_tokens=(usage.prompt_tokens if usage else 0) or 0,
        output_tokens=(usage.completion_tokens if usage else 0) or 0,
        cost_usd=cost,
        stop_reason=finish,
        request_id=resp.id or "",
        duration_ms=int((timezone.now() - started).total_seconds() * 1000),
    )
    if finish == "content_filter":
        raise AIUnavailable("The AI declined this request.")
    if finish == "length":
        raise AIUnavailable("The AI response was cut off; try a smaller request or raise the model's limits.")
    if parsed is None:
        log.warning("AI returned unusable output for %s: %s", kind, error)
        raise AIUnavailable("The AI returned an answer Markette could not read; try again or pick another model.")
    return parsed


def _short_error(exc):
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("message")
        if msg:
            return str(msg)[:200]
    return str(exc)[:200]


def _local(dt, tz):
    return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M (%a)")


def _parse_local(value, tz):
    try:
        naive = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=tz)


def _channel_brief(channel, start, end, tz, min_gap):
    profile = engine.get_profile(channel)
    scores = profile.scores
    taken = list(
        PostTarget.objects.filter(
            channel=channel,
            scheduled_at__gte=start - timedelta(days=1),
            scheduled_at__lte=end + timedelta(days=1),
            status__in=(TargetStatus.SCHEDULED, TargetStatus.QUEUED, TargetStatus.PUBLISHED),
        ).values_list("scheduled_at", flat=True)
    )
    # Best free candidate moments inside the window, according to the engine.
    candidates = []
    for _ in range(12):
        slot = engine.next_best_slot(channel, start, end, taken + [c for c, _ in candidates], min_gap)
        if slot is None:
            break
        candidates.append((slot, scores[slot.weekday()][slot.hour]))
    best_by_day = []
    for d in range(7):
        top = sorted(range(24), key=lambda h: scores[d][h], reverse=True)[:3]
        best_by_day.append(f"{WEEKDAYS[d]} {', '.join(f'{h:02d}h' for h in sorted(top))}")
    caps = get_provider(channel.provider).capabilities
    lines = [
        f'- channel_id {channel.id}: {channel.get_provider_display()} "{channel.name}" ({caps.kind}), '
        f"{channel.followers_count or 'unknown'} followers; history: {profile.sample_size} measured posts"
        f"{' (mostly platform priors)' if profile.sample_size < 10 else ''}",
        f"  best hours per weekday: {'; '.join(best_by_day)}",
        f"  already scheduled/published nearby: {', '.join(_local(t, tz) for t in sorted(taken)) or 'none'}",
        "  engine's top free moments in window: "
        + (", ".join(f"{_local(s, tz)} score {v:.0f}" for s, v in candidates) or "none"),
    ]
    return "\n".join(lines), taken


def _valid_choice(when, start, end, taken, min_gap):
    if when is None or not (start - timedelta(minutes=1) <= when <= end + timedelta(minutes=1)):
        return False
    return not any(abs((when - t).total_seconds()) < min_gap.total_seconds() for t in taken)


# --- features ----------------------------------------------------------------


def suggest_times(workspace, user, channels, text, title="", start=None, end=None, campaign=None):
    tz = workspace.tz
    now = timezone.now()
    start = max(start or now, now + timedelta(minutes=5))
    end = end or start + timedelta(days=7)
    min_gap = timedelta(minutes=workspace.min_gap_minutes)
    briefs, taken_by = [], {}
    for ch in channels:
        b, taken = _channel_brief(ch, start, end, tz, min_gap)
        briefs.append(b)
        taken_by[ch.id] = taken
    prompt = f"""Choose when to publish this content on each channel.

Now: {_local(now, tz)}. Time zone: {workspace.timezone}.
Window: {_local(start, tz)} → {_local(end, tz)}. Minimum gap between posts on the same channel: {workspace.min_gap_minutes} minutes.
{f"Campaign: {campaign.name} ({campaign.starts_on} → {campaign.ends_on}). Brief: {campaign.brief}" if campaign else ""}

Content{f" — title: {title}" if title else ""}:
<content>
{text[:6000]}
</content>

Channels:
{chr(10).join(briefs)}

Return exactly one choice per channel_id. Prefer the engine's top free moments unless the content gives a reason
not to (a deadline, an event time, a weekend-only topic, staggering the same message across networks so they do not
all land in the same hour). Put local_time in the workspace time zone."""
    result = _call("suggest_times", workspace, user, prompt, TimeSuggestions)
    by_channel = {c.channel_id: c for c in result.choices}
    out = []
    for ch in channels:
        choice = by_channel.get(ch.id)
        when = _parse_local(choice.local_time, tz) if choice else None
        if _valid_choice(when, start, end, taken_by[ch.id], min_gap):
            out.append({"channel": ch.id, "scheduled_at": when, "rationale": choice.rationale, "source": "ai"})
            continue
        fallback = engine.next_best_slot(ch, start, end, taken_by[ch.id], min_gap)
        if fallback:
            out.append(
                {
                    "channel": ch.id,
                    "scheduled_at": fallback,
                    "source": "engine",
                    "rationale": f"Best-scoring free slot: {engine.describe_slot(fallback, engine.get_profile(ch).scores)}",
                }
            )
    return {"summary": result.summary, "suggestions": out}


def plan_campaign(workspace, user, channels, brief, start, end, posts_per_week=3, language=""):
    tz = workspace.tz
    min_gap = timedelta(minutes=workspace.min_gap_minutes)
    now = timezone.now()
    start = max(start, now + timedelta(minutes=30))
    briefs, taken_by = [], {}
    for ch in channels:
        b, taken = _channel_brief(ch, start, end, tz, min_gap)
        briefs.append(b)
        taken_by[ch.id] = taken
    limits = "\n".join(
        f"- channel_id {ch.id}: {ch.get_provider_display()}, max {get_provider(ch.provider).capabilities.max_chars or 'no'} "
        f"characters{', needs a title (blog)' if get_provider(ch.provider).capabilities.supports_title else ''}"
        f"{', needs an image (the human will attach it)' if get_provider(ch.provider).capabilities.requires_media else ''}"
        for ch in channels
    )
    prompt = f"""Plan a publishing campaign.

Now: {_local(now, tz)}. Time zone: {workspace.timezone}.
Period: {_local(start, tz)} → {_local(end, tz)}. Target cadence: about {posts_per_week} posts per week overall.
Minimum gap between posts on the same channel: {workspace.min_gap_minutes} minutes.
{f"Write the copy in {language}." if language else "Write the copy in the language of the brief."}

Brief:
<brief>
{brief[:8000]}
</brief>

Channels and limits:
{limits}

Timing data:
{chr(10).join(briefs)}

Design an arc over the period (e.g. announce → explain → social proof → last call), not repeated copies. Each post
may go to a subset of channels; adapt the copy to each network's norms and length limit. Schedule each publication at
a strong moment for that channel, staggered across networks."""
    plan = _call("plan_campaign", workspace, user, prompt, CampaignPlan, max_tokens=32000)
    valid_ids = {c.id for c in channels}
    posts = []
    for p in plan.posts:
        pubs = []
        for pub in p.publications:
            if pub.channel_id not in valid_ids:
                continue
            when = _parse_local(pub.local_time, tz)
            if not _valid_choice(when, start, end, taken_by[pub.channel_id], min_gap):
                ch = next(c for c in channels if c.id == pub.channel_id)
                when = engine.next_best_slot(ch, start, end, taken_by[pub.channel_id], min_gap)
                if when is None:
                    continue
            taken_by[pub.channel_id].append(when)
            pubs.append({"channel": pub.channel_id, "scheduled_at": when, "body": pub.body, "title": pub.title})
        if pubs:
            posts.append({"angle": p.angle, "publications": pubs})
    return {"strategy": plan.strategy, "posts": posts}


def adapt_content(workspace, user, channels, text, title="", instructions=""):
    specs = []
    for ch in channels:
        caps = get_provider(ch.provider).capabilities
        specs.append(
            f"- channel_id {ch.id}: {ch.get_provider_display()} ({caps.kind}); "
            f"max {caps.max_chars or 'unlimited'} characters"
            + ("; URLs count as 23 characters" if caps.count_mode in ("x", "mastodon") else "")
            + ("; needs a title, body is Markdown" if caps.supports_title else "")
        )
    prompt = f"""Adapt this content for each channel. Keep the facts, links and calls to action; change length, tone,
structure and hashtags to suit each network. Never exceed the character limit.
{f"Extra instructions: {instructions}" if instructions else ""}

{f"Title: {title}" if title else ""}
<content>
{text[:12000]}
</content>

Channels:
{chr(10).join(specs)}

Return one variant per channel_id. Put any caveat for the human (e.g. "add an image for Instagram") in notes."""
    result = _call("adapt_content", workspace, user, prompt, Adaptation)
    valid = {c.id: c for c in channels}
    out = []
    for v in result.variants:
        ch = valid.get(v.channel_id)
        if not ch:
            continue
        from apps.channels.providers.text import measure

        caps = get_provider(ch.provider).capabilities
        length = measure(v.body, caps.count_mode)
        out.append(
            {
                "channel": ch.id,
                "body": v.body,
                "title": v.title,
                "notes": v.notes,
                "length": length,
                "over_limit": bool(caps.max_chars and length > caps.max_chars),
            }
        )
    return {"variants": out}

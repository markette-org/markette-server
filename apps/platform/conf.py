"""Effective configuration: superadmin overrides (database) on top of the environment.

Use ``conf.get("X_CLIENT_ID")`` instead of ``settings.X_CLIENT_ID`` for anything
listed in OVERRIDABLE or SECRETS. Values are cached per process for a few
seconds so web and worker processes pick up changes quickly without a DB hit
per call.
"""

import time

from django.conf import settings

# name -> type used to coerce what the admin typed
OVERRIDABLE = {
    "X_CLIENT_ID": str,
    "LINKEDIN_CLIENT_ID": str,
    "LINKEDIN_API_VERSION": str,
    "LINKEDIN_ORG_SCOPES": bool,
    "META_APP_ID": str,
    "META_GRAPH_VERSION": str,
    "MASTODON_APP_NAME": str,
    "THREADS_APP_ID": str,
    "GOOGLE_CLIENT_ID": str,
    "REDDIT_CLIENT_ID": str,
    "REDDIT_USER_AGENT": str,
    "SLACK_CLIENT_ID": str,
    "AI_ENABLED": bool,
    "AI_PROVIDER": str,
    "AI_BASE_URL": str,
    "AI_STRUCTURED_OUTPUT": str,
    "AI_MODEL": str,
    "AI_EFFORT": str,
    "AI_PRICE_INPUT_PER_MTOK": float,
    "AI_PRICE_OUTPUT_PER_MTOK": float,
    "PUBLISH_MAX_ATTEMPTS": int,
}
SECRETS = (
    "X_CLIENT_SECRET",
    "LINKEDIN_CLIENT_SECRET",
    "META_APP_SECRET",
    "THREADS_APP_SECRET",
    "GOOGLE_CLIENT_SECRET",
    "REDDIT_CLIENT_SECRET",
    "SLACK_CLIENT_SECRET",
    "AI_API_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
)
AI_PROVIDERS = ("openrouter", "openai_compatible")
AI_EFFORTS = ("minimal", "low", "medium", "high")
AI_OUTPUT_MODES = ("json_schema", "json_object", "prompt")
TTL = 15.0

_cache = {"at": 0.0, "row": None}


def invalidate():
    _cache["at"] = 0.0


def row():
    if time.monotonic() - _cache["at"] > TTL or _cache["row"] is None:
        from .models import PlatformSettings

        try:
            _cache["row"] = PlatformSettings.load()
        except Exception:  # noqa: BLE001 - before migrations, fall back to the environment
            return None
        _cache["at"] = time.monotonic()
    return _cache["row"]


def _env(name):
    return getattr(settings, name, None)


def source(name):
    r = row()
    if r is not None:
        store = r.secrets if name in SECRETS else r.overrides
        value = store.get(name)
        if value not in (None, ""):
            return "database"
    return "environment"


def get(name):
    r = row()
    if r is not None:
        store = r.secrets if name in SECRETS else r.overrides
        value = store.get(name)
        if value not in (None, ""):
            return value
    if name == "AI_ENABLED":
        # Without an explicit choice, AI is on whenever a key exists somewhere.
        if settings.AI_ENABLED or (r is not None and r.secrets.get("AI_API_KEY")):
            return True
        # Local OpenAI-compatible servers (Ollama, vLLM) usually need no key: a base URL is enough.
        return get("AI_PROVIDER") == "openai_compatible" and bool(get("AI_BASE_URL"))
    return _env(name)


def coerce(name, value):
    if value in (None, ""):
        return None
    kind = OVERRIDABLE[name]
    if kind is bool:
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes", "on")
    return kind(value)


def publishing_paused():
    r = row()
    return bool(r and r.publishing_paused)

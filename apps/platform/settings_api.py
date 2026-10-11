"""Superadmin settings page API, plus the public subset every visitor needs."""

import openai
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.channels.providers.base import redirect_uri
from apps.core.docs import doc

from . import conf
from .models import PlatformSettings
from .views import IsSuperAdmin

PLAIN_FIELDS = (
    "instance_name",
    "support_email",
    "announcement",
    "announcement_level",
    "signups_open",
    "org_creation_open",
    "signup_domains",
    "publishing_paused",
    "default_max_workspaces",
    "default_max_channels",
    "default_max_members",
    "default_ai_monthly_tokens",
)
NULLABLE_INTS = ("default_max_workspaces", "default_max_channels", "default_max_members", "default_ai_monthly_tokens")


def _describe(ps):
    data = {f: getattr(ps, f) for f in PLAIN_FIELDS}
    data["overrides"] = {
        name: {
            "value": ps.overrides.get(name),
            "effective": conf.get(name),
            "source": conf.source(name),
        }
        for name in conf.OVERRIDABLE
    }
    # Secrets are write-only: say whether one is set and where it comes from, never the value.
    data["secrets"] = {
        name: {"set": bool(conf.get(name)), "source": conf.source(name) if conf.get(name) else None}
        for name in conf.SECRETS
    }
    # What to register with each network: built from PUBLIC_API_URL, exactly as the OAuth flows use it.
    data["callback_urls"] = {
        key: redirect_uri(key) for key in ("x", "linkedin", "facebook", "threads", "youtube", "reddit", "slack")
    }
    data["stripe_webhook_url"] = f"{settings.PUBLIC_API_URL}/api/billing/webhook/"
    data["ai_efforts"] = list(conf.AI_EFFORTS)
    data["ai_providers"] = list(conf.AI_PROVIDERS)
    data["ai_output_modes"] = list(conf.AI_OUTPUT_MODES)
    data["updated_at"] = ps.updated_at
    data["updated_by"] = (ps.updated_by.name or ps.updated_by.email) if ps.updated_by else None
    return data


@doc("platform", "Installation settings", description="Values here override the environment; secrets are write-only.")
@api_view(["GET", "PATCH"])
@permission_classes([IsSuperAdmin])
def platform_settings(request):
    ps = PlatformSettings.load()
    if request.method == "PATCH":
        body = request.data
        errors = {}
        for f in PLAIN_FIELDS:
            if f not in body:
                continue
            value = body[f]
            if f in NULLABLE_INTS:
                if value in (None, ""):
                    value = None
                else:
                    try:
                        value = int(value)
                        if value < 0:
                            raise ValueError
                    except (TypeError, ValueError):
                        errors[f] = "A whole number ≥ 0, or empty for unlimited."
                        continue
            elif f in ("signups_open", "org_creation_open", "publishing_paused"):
                if not isinstance(value, bool):
                    errors[f] = "Must be true or false."
                    continue
            elif f == "support_email" and value:
                try:
                    validate_email(value)
                except DjangoValidationError:
                    errors[f] = "Enter a valid email address."
                    continue
            elif f == "signup_domains":
                domains = [d.strip().lower().lstrip("@") for d in str(value or "").split(",") if d.strip()]
                if any("." not in d or " " in d for d in domains):
                    errors[f] = "Comma-separated domains, e.g. acme.com, acme.org."
                    continue
                value = ", ".join(dict.fromkeys(domains))
            elif f == "announcement_level" and value not in ("info", "warning", "critical"):
                errors[f] = "info, warning or critical."
                continue
            elif f == "instance_name" and not str(value).strip():
                errors[f] = "Required."
                continue
            setattr(ps, f, value)
        overrides = dict(ps.overrides)
        for name, value in (body.get("overrides") or {}).items():
            if name not in conf.OVERRIDABLE:
                errors[f"overrides.{name}"] = "Not an editable setting."
                continue
            try:
                coerced = conf.coerce(name, value)
            except (TypeError, ValueError):
                errors[f"overrides.{name}"] = "Invalid value."
                continue
            if name == "AI_PROVIDER" and coerced and coerced not in conf.AI_PROVIDERS:
                errors[f"overrides.{name}"] = f"One of {', '.join(conf.AI_PROVIDERS)}."
                continue
            if name == "AI_STRUCTURED_OUTPUT" and coerced and coerced not in conf.AI_OUTPUT_MODES:
                errors[f"overrides.{name}"] = f"One of {', '.join(conf.AI_OUTPUT_MODES)}."
                continue
            if name == "AI_BASE_URL" and coerced and not str(coerced).startswith(("http://", "https://")):
                errors[f"overrides.{name}"] = "A URL starting with http:// or https://."
                continue
            if name == "AI_EFFORT" and coerced and coerced not in conf.AI_EFFORTS:
                errors[f"overrides.{name}"] = f"One of {', '.join(conf.AI_EFFORTS)}."
                continue
            if name == "PUBLISH_MAX_ATTEMPTS" and coerced is not None and not 1 <= coerced <= 20:
                errors[f"overrides.{name}"] = "Between 1 and 20."
                continue
            if coerced is None:
                overrides.pop(name, None)  # back to the environment
            else:
                overrides[name] = coerced
        secrets = dict(ps.secrets)
        for name, value in (body.get("secrets") or {}).items():
            if name not in conf.SECRETS:
                errors[f"secrets.{name}"] = "Not an editable secret."
            elif value in (None, ""):
                secrets.pop(name, None)  # cleared: fall back to the environment
            else:
                secrets[name] = str(value).strip()
        if errors:
            raise ValidationError(errors)
        ps.overrides, ps.secrets, ps.updated_by = overrides, secrets, request.user
        ps.save()
    return Response(_describe(ps))


@doc("platform", "Test the AI provider connection")
@api_view(["POST"])
@permission_classes([IsSuperAdmin])
def test_ai(request):
    """Check the configured provider, key and model without generating anything."""
    from apps.insights.ai import AIUnavailable, base_url, make_client

    model = conf.get("AI_MODEL")
    if conf.get("AI_PROVIDER") == "openrouter" and not conf.get("AI_API_KEY"):
        return Response({"ok": False, "detail": "No OpenRouter API key is configured."})
    try:
        client = make_client(timeout=20.0, max_retries=1)
        if conf.get("AI_PROVIDER") == "openrouter":
            # /key validates the key and reports its credit; /models is public and would not.
            key_info = client.get("/key", cast_to=object)
            label = (key_info or {}).get("data", {}).get("label") if isinstance(key_info, dict) else None
        else:
            label = None
        ids = [m.id for m in client.models.list()]
    except AIUnavailable as exc:
        return Response({"ok": False, "detail": str(exc)})
    except openai.AuthenticationError:
        return Response({"ok": False, "detail": "The API key was rejected."})
    except openai.APIConnectionError:
        return Response({"ok": False, "detail": f"Could not connect to {base_url()}."})
    except openai.APIError as exc:
        return Response({"ok": False, "detail": f"The provider answered with an error: {exc.__class__.__name__}."})
    prefix = f"Key accepted{f' ({label})' if label else ''}. "
    if model == "openrouter/auto" or model in ids:
        return Response({"ok": True, "detail": prefix + f"Model {model} is available.", "model": model})
    if not ids:
        return Response(
            {"ok": True, "detail": prefix + "The server lists no models; make sure the model name is right."}
        )
    return Response(
        {"ok": False, "detail": prefix + f"Model {model} is not offered by this provider.", "models": ids[:50]}
    )


@doc(
    "auth",
    "Public installation info",
    description="Name, announcement banner and sign-up rules. No authentication needed.",
)
@api_view(["GET"])
@permission_classes([AllowAny])
def public_settings(request):
    ps = PlatformSettings.load()
    return Response(
        {
            "instance_name": ps.instance_name,
            "support_email": ps.support_email,
            "announcement": ps.announcement,
            "announcement_level": ps.announcement_level,
            "signups_open": ps.signups_open,
            "org_creation_open": ps.org_creation_open,
            "signup_domains": [d.strip() for d in ps.signup_domains.split(",") if d.strip()],
        }
    )

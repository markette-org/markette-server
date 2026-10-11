"""Markette settings. Everything deployment-specific comes from the environment."""

import os
from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, str(default)).lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [v.strip() for v in env(name, default).split(",") if v.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
if not DEBUG and SECRET_KEY in ("dev-insecure-change-me", "change-me", "build"):
    import sys

    if "collectstatic" not in sys.argv:
        raise RuntimeError("Set DJANGO_SECRET_KEY to a long random value in production.")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

# Public URL of this API (used for OAuth redirect URIs and for media URLs that
# networks such as Instagram must fetch themselves).
PUBLIC_API_URL = env("PUBLIC_API_URL", "http://localhost:8000").rstrip("/")
# Public URL of the Angular app (OAuth flows redirect back here).
FRONTEND_URL = env("FRONTEND_URL", "http://localhost:4200").rstrip("/")

# Behind the nginx container / a TLS-terminating proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# Key used to encrypt channel credentials at rest (Fernet, urlsafe base64, 32 bytes).
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CREDENTIALS_KEY = env("CREDENTIALS_KEY", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "corsheaders",
    "drf_spectacular",
    "apps.core",
    "apps.tenants",
    "apps.channels",
    "apps.publishing",
    "apps.analytics",
    "apps.insights",
    "apps.teams",
    "apps.platform",
    "apps.billing",
    "apps.sso",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "markette.urls"
WSGI_APPLICATION = "markette.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "markette"),
        "USER": env("POSTGRES_USER", "markette"),
        "PASSWORD": env("POSTGRES_PASSWORD", "markette"),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

AUTH_USER_MODEL = "core.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", BASE_DIR / "media"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
MAX_MEDIA_UPLOAD_BYTES = int(env("MAX_MEDIA_UPLOAD_BYTES", 200 * 1024 * 1024))

# The mobile apps (Capacitor) load the UI from these local origins and call the API cross-origin.
MOBILE_APP_ORIGINS = ["capacitor://localhost", "https://localhost", "http://localhost", "ionic://localhost"]
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", FRONTEND_URL) + MOBILE_APP_ORIGINS
CORS_ALLOW_HEADERS = (
    "accept",
    "authorization",
    "content-type",
    "x-workspace",
    "x-requested-with",
)

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.core.authentication.ApiTokenAuthentication",
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
        "rest_framework.filters.SearchFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    # Keeps User.last_login meaningful (platform "active users").
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Markette API",
    "DESCRIPTION": "",  # set below from markette.openapi
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # The reference is public; calling the API still needs a token.
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
    "SERVE_AUTHENTICATION": [],
    "SCHEMA_PATH_PREFIX": r"/api/",
    "COMPONENT_SPLIT_REQUEST": True,
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "markette.api_docs.add_workspace_header",
    ],
    "ENUM_NAME_OVERRIDES": {
        "PostStatusEnum": "apps.publishing.models.PostStatus",
        "TargetStatusEnum": "apps.publishing.models.TargetStatus",
        "ChannelStatusEnum": "apps.channels.models.ChannelStatus",
        "WorkspaceRoleEnum": "apps.tenants.models.Role",
        "OrgRoleEnum": "apps.tenants.models.OrgRole",
        "OrgStatusEnum": "apps.tenants.models.OrgStatus",
    },
    "SWAGGER_UI_SETTINGS": {"persistAuthorization": True, "displayOperationId": False, "filter": True},
}

# --- Celery ------------------------------------------------------------------
CELERY_BROKER_URL = env("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = None
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 15 * 60
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULE = {
    # Hands every due post target to a worker. Runs often so the effective
    # scheduling precision is ~SCHEDULER_TICK_SECONDS.
    "dispatch-due-targets": {
        "task": "apps.publishing.tasks.dispatch_due_targets",
        "schedule": float(env("SCHEDULER_TICK_SECONDS", 15)),
    },
    "requeue-stuck-targets": {
        "task": "apps.publishing.tasks.requeue_stuck_targets",
        "schedule": 300.0,
    },
    "collect-due-metrics": {
        "task": "apps.analytics.tasks.collect_due_metrics",
        "schedule": 300.0,
    },
    "snapshot-channel-audience": {
        "task": "apps.analytics.tasks.snapshot_channel_audiences",
        "schedule": crontab(minute=17, hour=3),
    },
    "refresh-expiring-tokens": {
        "task": "apps.channels.tasks.refresh_expiring_tokens",
        "schedule": 600.0,
    },
    "rebuild-best-times": {
        "task": "apps.insights.tasks.rebuild_all_best_times",
        "schedule": crontab(minute=40, hour=4),
    },
}

# --- Publishing --------------------------------------------------------------
PUBLISH_MAX_ATTEMPTS = int(env("PUBLISH_MAX_ATTEMPTS", 5))
HUGO_WORKDIR = Path(env("HUGO_WORKDIR", BASE_DIR / "var" / "hugo"))

# --- Provider credentials ----------------------------------------------------
X_CLIENT_ID = env("X_CLIENT_ID", "")
X_CLIENT_SECRET = env("X_CLIENT_SECRET", "")
LINKEDIN_CLIENT_ID = env("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET = env("LINKEDIN_CLIENT_SECRET", "")
LINKEDIN_API_VERSION = env("LINKEDIN_API_VERSION", "202509")
# Organization (company page) scopes need LinkedIn's Community Management API product.
LINKEDIN_ORG_SCOPES = env_bool("LINKEDIN_ORG_SCOPES", False)
META_APP_ID = env("META_APP_ID", "")
META_APP_SECRET = env("META_APP_SECRET", "")
META_GRAPH_VERSION = env("META_GRAPH_VERSION", "v23.0")
MASTODON_APP_NAME = env("MASTODON_APP_NAME", "Markette")
# Threads has its own app id/secret inside a Meta app (Threads use case).
THREADS_APP_ID = env("THREADS_APP_ID", "")
THREADS_APP_SECRET = env("THREADS_APP_SECRET", "")
# Google Cloud OAuth client for YouTube (YouTube Data API v3 enabled).
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET", "")
# Reddit "web app" (approved under the Responsible Builder Policy)
REDDIT_CLIENT_ID = env("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = env("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = env("REDDIT_USER_AGENT", "")
# Slack app for "Add to Slack" (optional: without it users paste a bot token)
SLACK_CLIENT_ID = env("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = env("SLACK_CLIENT_SECRET", "")

# --- Billing (Stripe) -------------------------------------------------------
# Also editable in Platform → Settings. Webhook endpoint: {PUBLIC_API_URL}/api/billing/webhook/
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", "")

# --- MCP server ---------------------------------------------------------------
# Public URL of the MCP endpoint (served by the "mcp" container behind nginx at /mcp).
PUBLIC_MCP_URL = env("PUBLIC_MCP_URL", f"{PUBLIC_API_URL}/mcp")

# --- AI ----------------------------------------------------------------------
# Any OpenAI-compatible Chat Completions API. "openrouter" uses https://openrouter.ai/api/v1
# unless AI_BASE_URL is set; "openai_compatible" requires AI_BASE_URL (OpenAI, Azure, vLLM,
# Ollama, LiteLLM…). AI features are disabled — and the statistical engine is used alone —
# when no key is configured. All of these can be changed in Platform → Settings.
AI_PROVIDER = env("AI_PROVIDER", "openrouter")
AI_BASE_URL = env("AI_BASE_URL", "")
AI_API_KEY = env("AI_API_KEY", "")
AI_ENABLED = env_bool("AI_ENABLED", bool(AI_API_KEY))
# How to ask for JSON: json_schema (best), json_object, or prompt (instructions only).
AI_STRUCTURED_OUTPUT = env("AI_STRUCTURED_OUTPUT", "json_schema")
AI_MODEL = env("AI_MODEL", "openrouter/auto")
# Optional reasoning effort (low/medium/high) for models that support it; empty = model default.
AI_EFFORT = env("AI_EFFORT", "")
# Estimate AI cost when the provider does not report it (USD per million tokens).
AI_PRICE_INPUT_PER_MTOK = float(env("AI_PRICE_INPUT_PER_MTOK", 0))
AI_PRICE_OUTPUT_PER_MTOK = float(env("AI_PRICE_OUTPUT_PER_MTOK", 0))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}

import markette.api_docs as _api_docs  # noqa: E402

SPECTACULAR_SETTINGS["DESCRIPTION"] = _api_docs.DESCRIPTION
SPECTACULAR_SETTINGS["TAGS"] = _api_docs.TAGS

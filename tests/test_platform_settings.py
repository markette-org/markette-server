import pytest
from rest_framework.test import APIClient

from apps.channels.providers.x import XProvider
from apps.core.models import User
from apps.platform import conf
from apps.platform.models import PlatformSettings
from apps.publishing.tasks import dispatch_due_targets
from apps.tenants.models import Organization


@pytest.fixture
def root(db):
    c = APIClient()
    c.force_authenticate(User.objects.create_superuser("root@example.com", "pw-pw-pw-pw-1"))
    return c


@pytest.fixture(autouse=True)
def _fresh_conf():
    conf.invalidate()
    yield
    conf.invalidate()


def test_only_superadmins(api):
    assert api.get("/api/platform/settings/").status_code == 403


def test_overrides_and_secrets_are_layered_on_env(root, settings):
    settings.X_CLIENT_ID = ""
    assert XProvider.is_configured() is False
    r = root.patch(
        "/api/platform/settings/",
        {
            "overrides": {"X_CLIENT_ID": "abc", "AI_EFFORT": "high", "PUBLISH_MAX_ATTEMPTS": "3"},
            "secrets": {"X_CLIENT_SECRET": "s3cr3t"},
        },
        format="json",
    )
    assert r.status_code == 200, r.data
    assert r.data["overrides"]["X_CLIENT_ID"] == {"value": "abc", "effective": "abc", "source": "database"}
    assert r.data["overrides"]["PUBLISH_MAX_ATTEMPTS"]["effective"] == 3
    assert r.data["secrets"]["X_CLIENT_SECRET"] == {"set": True, "source": "database"}
    assert "s3cr3t" not in str(r.data)
    assert XProvider.is_configured() is True
    # Clearing falls back to the environment.
    r = root.patch(
        "/api/platform/settings/", {"overrides": {"X_CLIENT_ID": ""}, "secrets": {"X_CLIENT_SECRET": ""}}, format="json"
    )
    assert r.data["overrides"]["X_CLIENT_ID"]["source"] == "environment"
    assert r.data["secrets"]["X_CLIENT_SECRET"]["set"] is False
    assert (
        root.patch("/api/platform/settings/", {"overrides": {"AI_EFFORT": "turbo"}}, format="json").status_code == 400
    )


def test_ai_key_in_database_enables_ai(root, settings):
    settings.AI_ENABLED = False
    assert conf.get("AI_ENABLED") is False
    root.patch("/api/platform/settings/", {"secrets": {"AI_API_KEY": "sk-or-test"}}, format="json")
    assert conf.get("AI_ENABLED") is True
    root.patch("/api/platform/settings/", {"overrides": {"AI_ENABLED": False}}, format="json")
    assert conf.get("AI_ENABLED") is False


def test_signup_rules_and_default_limits(root):
    root.patch("/api/platform/settings/", {"signup_domains": "acme.com", "default_max_channels": 5}, format="json")
    anon = APIClient()
    payload = {"password": "a-long-passphrase", "organization_name": "X"}
    assert anon.post("/api/auth/register/", {**payload, "email": "a@other.com"}, format="json").status_code == 400
    assert anon.post("/api/auth/register/", {**payload, "email": "a@acme.com"}, format="json").status_code == 201
    assert Organization.objects.get(name="X").max_channels == 5
    root.patch("/api/platform/settings/", {"signups_open": False}, format="json")
    assert anon.post("/api/auth/register/", {**payload, "email": "b@acme.com"}, format="json").status_code == 403
    pub = anon.get("/api/platform/public/").data
    assert pub["signups_open"] is False and pub["signup_domains"] == ["acme.com"]
    # Superadmin-created orgs get defaults unless limits are given.
    r = root.post("/api/platform/organizations/", {"name": "Y", "max_channels": 50}, format="json")
    assert r.data["max_channels"] == 50


def test_pause_stops_dispatching(root, api, channel):
    root.patch("/api/platform/settings/", {"publishing_paused": True}, format="json")
    post = api.post("/api/posts/", {"body": "x", "targets": [{"channel": channel.id}]}, format="json").data
    api.post(f"/api/posts/{post['id']}/schedule/", {"mode": "now"}, format="json")
    assert dispatch_due_targets() == 0
    assert root.get("/api/platform/health/").data["status"] == "paused"
    assert PlatformSettings.load().publishing_paused is True


def test_test_ai_without_key(root, settings):
    settings.AI_PROVIDER, settings.AI_API_KEY = "openrouter", ""
    r = root.post("/api/platform/settings/test-ai/").data
    assert r == {"ok": False, "detail": "No OpenRouter API key is configured."}


def test_ai_provider_settings_are_validated(root):
    bad = root.patch(
        "/api/platform/settings/",
        {"overrides": {"AI_PROVIDER": "anthropic", "AI_BASE_URL": "localhost:11434", "AI_STRUCTURED_OUTPUT": "xml"}},
        format="json",
    )
    assert set(bad.data) == {"overrides.AI_PROVIDER", "overrides.AI_BASE_URL", "overrides.AI_STRUCTURED_OUTPUT"}
    ok = root.patch(
        "/api/platform/settings/",
        {
            "overrides": {
                "AI_PROVIDER": "openai_compatible",
                "AI_BASE_URL": "http://ollama:11434/v1",
                "AI_MODEL": "llama3.3",
            }
        },
        format="json",
    )
    assert ok.status_code == 200
    # A local server needs no key: a base URL is enough to enable AI.
    assert conf.get("AI_ENABLED") is True


def test_plain_fields_are_validated(root):
    bad = root.patch(
        "/api/platform/settings/",
        {"signups_open": "false", "support_email": "nope", "signup_domains": "acme"},
        format="json",
    )
    assert set(bad.data) == {"signups_open", "support_email", "signup_domains"}
    ok = root.patch("/api/platform/settings/", {"signup_domains": " @Acme.com, acme.com ,b.org"}, format="json")
    assert ok.data["signup_domains"] == "acme.com, b.org"
    assert ok.data["callback_urls"]["facebook"].endswith("/api/oauth/facebook/callback")

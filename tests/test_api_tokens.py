from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import ApiToken


def test_issue_use_and_revoke_token(api, user, workspace):
    r = api.post("/api/auth/tokens/", {"name": "Claude Desktop", "expires_in_days": 30}, format="json")
    assert r.status_code == 201 and r.data["token"].startswith("mkt_")
    raw = r.data["token"]
    stored = ApiToken.objects.get()
    assert stored.token_hash != raw and raw.startswith(stored.hint)
    assert "token" not in api.get("/api/auth/tokens/").data[0]
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}", HTTP_X_WORKSPACE=workspace.slug)
    assert c.get("/api/auth/me/").data["email"] == user.email
    assert c.get("/api/posts/").status_code == 200
    assert ApiToken.objects.get().last_used_at is not None
    assert api.delete(f"/api/auth/tokens/{stored.id}/").status_code == 204
    assert c.get("/api/auth/me/").status_code == 401


def test_expired_or_unknown_tokens_are_rejected(user):
    token, raw = ApiToken.issue(user, "old", expires_at=timezone.now() - timedelta(minutes=1))
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert c.get("/api/auth/me/").status_code == 401
    c.credentials(HTTP_AUTHORIZATION="Bearer mkt_nope")
    assert c.get("/api/auth/me/").status_code == 401
    user.is_active = False
    user.save()
    _, raw2 = ApiToken.issue(user, "inactive")
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {raw2}")
    assert c.get("/api/auth/me/").status_code == 401

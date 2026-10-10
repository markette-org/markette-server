import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient

from apps.billing import service
from apps.billing.models import Plan, Subscription
from apps.core.models import User
from apps.insights import ai
from apps.insights.models import AIRequest
from apps.platform import conf
from apps.platform.models import PlatformSettings
from apps.tenants.models import OrgMembership, OrgRole

SECRET = "whsec_test"


@pytest.fixture(autouse=True)
def stripe_keys(settings):
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    settings.STRIPE_WEBHOOK_SECRET = SECRET
    conf.invalidate()
    yield
    conf.invalidate()


@pytest.fixture
def org_owner(user, workspace):
    OrgMembership.objects.create(organization=workspace.organization, user=user, role=OrgRole.OWNER)
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def unlimited():
    return Plan.objects.create(
        name="Unlimited AI",
        slug="unlimited-ai",
        price_label="€29 / month",
        stripe_price_id="price_unl",
        ai_monthly_tokens=None,
        max_channels=50,
    )


def send(event):
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return APIClient().post(
        "/api/billing/webhook/", payload, content_type="application/json", HTTP_STRIPE_SIGNATURE=f"t={ts},v1={sig}"
    )


def sub_event(event_id, kind, org, status, price="price_unl"):
    return {
        "id": event_id,
        "type": kind,
        "data": {
            "object": {
                "id": "sub_1",
                "object": "subscription",
                "customer": "cus_1",
                "status": status,
                "cancel_at_period_end": False,
                "metadata": {"organization_id": str(org.pk)},
                "items": {"data": [{"price": {"id": price}, "current_period_end": 1893456000}]},
            }
        },
    }


def test_webhook_rejects_bad_signature():
    r = APIClient().post(
        "/api/billing/webhook/", "{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=deadbeef"
    )
    assert r.status_code == 400


def test_subscription_lifecycle_unlocks_and_reverts_unlimited_ai(workspace, user, unlimited, settings):
    org = workspace.organization
    ps = PlatformSettings.load()
    ps.default_ai_monthly_tokens = 1000
    ps.save()
    service.apply_limits(org, None)
    org.refresh_from_db()
    assert org.ai_monthly_tokens == 1000
    # Over the free allowance: AI is blocked.
    settings.AI_ENABLED = True
    AIRequest.objects.create(workspace=workspace, kind="x", model="m", prompt="", input_tokens=900, output_tokens=200)
    with pytest.raises(ai.AIUnavailable, match="allowance"):
        ai._call("adapt_content", workspace, user, "hi", ai.Adaptation)
    # Subscribe.
    assert send(sub_event("evt_1", "customer.subscription.created", org, "active")).data == {"result": "ok"}
    org.refresh_from_db()
    assert org.ai_monthly_tokens is None and org.max_channels == 50
    sub = Subscription.objects.get(organization=org)
    assert sub.plan == unlimited and sub.current_period_end.year == 2030
    # Redelivery is ignored.
    assert send(sub_event("evt_1", "customer.subscription.created", org, "active")).data == {"result": "duplicate"}
    # Cancelled → back to the default plan.
    send(sub_event("evt_2", "customer.subscription.deleted", org, "canceled"))
    org.refresh_from_db()
    assert org.ai_monthly_tokens == 1000 and Subscription.objects.get(organization=org).plan is None


def test_checkout_and_portal(org_owner, workspace, unlimited, monkeypatch):
    calls = []

    class Fake:
        def __init__(self):
            ns = SimpleNamespace
            self.v1 = ns(
                customers=ns(create=lambda params: calls.append(("customer", params)) or ns(id="cus_9")),
                checkout=ns(
                    sessions=ns(
                        create=lambda params: calls.append(("checkout", params))
                        or ns(url="https://checkout.stripe.test/s")
                    )
                ),
                billing_portal=ns(
                    sessions=ns(
                        create=lambda params: calls.append(("portal", params))
                        or ns(url="https://billing.stripe.test/p")
                    )
                ),
            )

    monkeypatch.setattr(service, "client", Fake)
    org = workspace.organization
    plans = org_owner.get("/api/billing/plans/").data
    assert plans["billing_enabled"] and plans["plans"][0]["purchasable"] is True
    r = org_owner.post(f"/api/billing/organizations/{org.pk}/checkout/", {"plan": unlimited.pk}, format="json")
    assert r.data == {"url": "https://checkout.stripe.test/s"}
    checkout = dict(calls)["checkout"]
    assert checkout["line_items"] == [{"price": "price_unl", "quantity": 1}]
    assert checkout["client_reference_id"] == str(org.pk) and checkout["customer"] == "cus_9"
    assert org_owner.post(f"/api/billing/organizations/{org.pk}/portal/").data["url"] == "https://billing.stripe.test/p"
    status = org_owner.get(f"/api/billing/organizations/{org.pk}/").data
    assert status["has_billing_account"] is True and status["plan"] is None


def test_only_org_admins_and_superadmin_plans(workspace, db, unlimited):
    member = User.objects.create_user("m@example.com", "pw-pw-pw-pw-1")
    OrgMembership.objects.create(organization=workspace.organization, user=member, role=OrgRole.MEMBER)
    c = APIClient()
    c.force_authenticate(member)
    assert c.get(f"/api/billing/organizations/{workspace.organization_id}/").status_code == 403
    assert c.get("/api/platform/plans/").status_code == 403
    root = APIClient()
    root.force_authenticate(User.objects.create_superuser("r@example.com", "pw-pw-pw-pw-1"))
    r = root.post("/api/platform/plans/", {"name": "Pro", "slug": "pro", "ai_monthly_tokens": 5_000_000}, format="json")
    assert r.status_code == 201


def test_plan_in_use_cannot_be_deleted_and_prices_must_be_positive(workspace, unlimited, db):
    Subscription.objects.create(organization=workspace.organization, plan=unlimited, status="active")
    root = APIClient()
    root.force_authenticate(User.objects.create_superuser("r2@example.com", "pw-pw-pw-pw-1"))
    assert root.delete(f"/api/platform/plans/{unlimited.pk}/").status_code == 400
    r = root.post(f"/api/platform/plans/{unlimited.pk}/stripe-price/", {"amount_cents": 0}, format="json")
    assert r.status_code == 400
    orgs = root.get("/api/platform/organizations/").data["results"]
    assert orgs[0]["subscription"]["plan"] == "Unlimited AI"

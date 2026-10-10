"""Stripe plumbing: checkout, customer portal and webhook-driven plan changes.

Plan limits are copied onto the Organization while its subscription is active,
and replaced by the platform's default limits when it ends, so the rest of the
app keeps reading limits from the organization only.
"""

import logging
from datetime import UTC, datetime

import stripe
from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.platform import conf
from apps.platform.models import PlatformSettings

from .models import Plan, StripeEvent, Subscription

log = logging.getLogger(__name__)
ACTIVE = ("active", "trialing", "past_due")  # past_due keeps access while Stripe retries the card


class BillingUnavailable(ValidationError):
    pass


def enabled():
    return bool(conf.get("STRIPE_SECRET_KEY"))


def client():
    key = conf.get("STRIPE_SECRET_KEY")
    if not key:
        raise BillingUnavailable({"detail": "Billing is not configured on this installation."})
    return stripe.StripeClient(key)


def _get(obj, key, default=None):
    """Read a field from a Stripe object or a plain dict (webhook payloads)."""
    if obj is None:
        return default
    try:
        return obj[key]
    except (KeyError, TypeError):
        return getattr(obj, key, default)


def apply_limits(org, plan):
    limits = plan.limits() if plan else PlatformSettings.load().default_limits()
    for k, v in limits.items():
        setattr(org, k, v)
    org.save(update_fields=[*limits.keys(), "updated_at"])


def checkout_url(org, plan, user):
    if not plan.stripe_price_id:
        raise ValidationError({"plan": "This plan has no Stripe price yet."})
    sub, _ = Subscription.objects.get_or_create(organization=org)
    if sub.is_active and sub.stripe_subscription_id:
        raise ValidationError(
            {"detail": "This organization already has a subscription; manage it in the billing portal."}
        )
    sc = client()
    if not sub.stripe_customer_id:
        customer = sc.v1.customers.create(
            params={"email": user.email, "name": org.name, "metadata": {"organization_id": str(org.pk)}}
        )
        sub.stripe_customer_id = customer.id
        sub.save(update_fields=["stripe_customer_id", "updated_at"])
    back = f"{settings.FRONTEND_URL}/organization?org={org.pk}"
    session = sc.v1.checkout.sessions.create(
        params={
            "mode": "subscription",
            "customer": sub.stripe_customer_id,
            "client_reference_id": str(org.pk),
            "line_items": [{"price": plan.stripe_price_id, "quantity": 1}],
            "subscription_data": {"metadata": {"organization_id": str(org.pk), "plan": plan.slug}},
            "metadata": {"organization_id": str(org.pk), "plan": plan.slug},
            "allow_promotion_codes": True,
            "success_url": f"{back}&billing=success",
            "cancel_url": f"{back}&billing=cancelled",
        }
    )
    return session.url


def portal_url(org):
    sub = Subscription.objects.filter(organization=org).first()
    if not sub or not sub.stripe_customer_id:
        raise ValidationError({"detail": "This organization has no billing account yet."})
    session = client().v1.billing_portal.sessions.create(
        params={"customer": sub.stripe_customer_id, "return_url": f"{settings.FRONTEND_URL}/organization?org={org.pk}"}
    )
    return session.url


def create_stripe_price(plan, amount_cents, currency, interval):
    """Create (or replace) the Stripe product+price behind a plan."""
    sc = client()
    product = sc.v1.products.create(params={"name": plan.name, "metadata": {"plan": plan.slug}})
    price = sc.v1.prices.create(
        params={
            "product": product.id,
            "unit_amount": int(amount_cents),
            "currency": currency.lower(),
            "recurring": {"interval": interval},
            "metadata": {"plan": plan.slug},
        }
    )
    plan.stripe_price_id = price.id
    plan.save(update_fields=["stripe_price_id", "updated_at"])
    return price.id


# --- webhooks -------------------------------------------------------------------


def construct_event(payload, signature):
    secret = conf.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise BillingUnavailable({"detail": "Webhook secret not configured."})
    return stripe.Webhook.construct_event(payload, signature, secret)


def _org_for(obj):
    from apps.tenants.models import Organization

    org_id = _get(_get(obj, "metadata", {}) or {}, "organization_id") or _get(obj, "client_reference_id")
    if org_id:
        org = Organization.objects.filter(pk=org_id).first()
        if org:
            return org
    customer = _get(obj, "customer")
    sub = (
        Subscription.objects.filter(stripe_customer_id=customer).select_related("organization").first()
        if customer
        else None
    )
    return sub.organization if sub else None


def _period_end(stripe_sub):
    ts = _get(stripe_sub, "current_period_end")
    if ts is None:
        # Newer API versions moved the period onto subscription items.
        items = _get(_get(stripe_sub, "items", {}), "data", []) or []
        ts = _get(items[0], "current_period_end") if items else None
    return datetime.fromtimestamp(ts, tz=UTC) if ts else None


def sync_subscription(stripe_sub):
    org = _org_for(stripe_sub)
    if org is None:
        log.warning("Stripe subscription %s matches no organization", _get(stripe_sub, "id"))
        return None
    items = _get(_get(stripe_sub, "items", {}), "data", []) or []
    price_id = _get(_get(items[0], "price", {}), "id") if items else None
    plan = Plan.objects.filter(stripe_price_id=price_id).first() if price_id else None
    status = _get(stripe_sub, "status", "")
    with transaction.atomic():
        sub, _ = Subscription.objects.select_for_update().get_or_create(organization=org)
        sub.stripe_subscription_id = _get(stripe_sub, "id") or sub.stripe_subscription_id
        sub.stripe_customer_id = _get(stripe_sub, "customer") or sub.stripe_customer_id
        sub.status = status
        sub.plan = plan if status in ACTIVE else None
        sub.current_period_end = _period_end(stripe_sub)
        sub.cancel_at_period_end = bool(_get(stripe_sub, "cancel_at_period_end", False))
        sub.save()
        apply_limits(org, sub.plan)
    log.info("Organization %s subscription %s → %s (%s)", org.pk, sub.stripe_subscription_id, status, plan)
    return sub


def handle_event(event):
    event_id, kind = _get(event, "id"), _get(event, "type")
    if StripeEvent.objects.filter(event_id=event_id).exists():
        return "duplicate"
    obj = _get(_get(event, "data", {}), "object")
    if kind == "checkout.session.completed" and _get(obj, "mode") == "subscription":
        org = _org_for(obj)
        if org:
            sub, _ = Subscription.objects.get_or_create(organization=org)
            sub.stripe_customer_id = _get(obj, "customer") or sub.stripe_customer_id
            sub.stripe_subscription_id = _get(obj, "subscription") or sub.stripe_subscription_id
            sub.save()
    elif kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        sync_subscription(obj)
    StripeEvent.objects.create(event_id=event_id, type=kind or "")
    return "ok"

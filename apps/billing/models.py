from django.db import models

from apps.core.models import TimeStamped
from apps.tenants.models import Organization


class Plan(TimeStamped):
    """A subscription plan sold through Stripe. Null limits mean unlimited."""

    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=80, unique=True)
    description = models.TextField(blank=True)
    # Display only (e.g. "€29 / month"); Stripe's price is the source of truth.
    price_label = models.CharField(max_length=60, blank=True)
    stripe_price_id = models.CharField(max_length=120, blank=True)
    max_workspaces = models.PositiveIntegerField(null=True, blank=True)
    max_channels = models.PositiveIntegerField(null=True, blank=True)
    max_members = models.PositiveIntegerField(null=True, blank=True)
    ai_monthly_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    is_public = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    sort = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort", "name"]

    def __str__(self):
        return self.name

    def limits(self):
        return {
            "max_workspaces": self.max_workspaces,
            "max_channels": self.max_channels,
            "max_members": self.max_members,
            "ai_monthly_tokens": self.ai_monthly_tokens,
        }


class Subscription(TimeStamped):
    """An organization's Stripe subscription, mirrored from webhooks."""

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(Plan, null=True, blank=True, on_delete=models.SET_NULL, related_name="subscriptions")
    stripe_customer_id = models.CharField(max_length=120, blank=True, db_index=True)
    stripe_subscription_id = models.CharField(max_length=120, blank=True, db_index=True)
    # Stripe status: active, trialing, past_due, canceled, unpaid, incomplete, incomplete_expired, paused.
    status = models.CharField(max_length=30, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)

    @property
    def is_active(self):
        return self.status in ("active", "trialing", "past_due")


class StripeEvent(models.Model):
    """Processed webhook events, so redeliveries are ignored."""

    event_id = models.CharField(max_length=120, unique=True)
    type = models.CharField(max_length=80)
    received_at = models.DateTimeField(auto_now_add=True)

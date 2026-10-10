from django.contrib import admin

from .models import Plan, StripeEvent, Subscription


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "price_label", "ai_monthly_tokens", "stripe_price_id", "active", "is_public")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("organization", "plan", "status", "current_period_end", "cancel_at_period_end")


admin.site.register(StripeEvent)

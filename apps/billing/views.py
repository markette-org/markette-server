import stripe
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action, api_view, authentication_classes, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.core.docs import doc, hidden
from apps.platform.views import IsSuperAdmin
from apps.tenants import limits
from apps.tenants.models import Organization, OrgMembership

from . import service
from .models import Plan, Subscription


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = (
            "id",
            "name",
            "slug",
            "description",
            "price_label",
            "stripe_price_id",
            "max_workspaces",
            "max_channels",
            "max_members",
            "ai_monthly_tokens",
            "is_public",
            "active",
            "sort",
            "created_at",
        )
        read_only_fields = ("id", "created_at")


class PublicPlanSerializer(PlanSerializer):
    purchasable = serializers.SerializerMethodField()

    class Meta(PlanSerializer.Meta):
        fields = (
            "id",
            "name",
            "slug",
            "description",
            "price_label",
            "max_workspaces",
            "max_channels",
            "max_members",
            "ai_monthly_tokens",
            "purchasable",
        )

    def get_purchasable(self, obj) -> bool:
        return bool(obj.stripe_price_id) and service.enabled()


@doc("billing", "Plans you can subscribe to")
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def plans(request):
    qs = Plan.objects.filter(active=True, is_public=True)
    return Response({"billing_enabled": service.enabled(), "plans": PublicPlanSerializer(qs, many=True).data})


def _org_admin(request, org_id):
    org = get_object_or_404(Organization, pk=org_id)
    m = OrgMembership.objects.filter(organization=org, user=request.user).first()
    if not (m and m.is_admin) and not request.user.is_superuser:
        raise PermissionDenied("Only organization owners and admins manage billing.")
    return org


@doc("billing", "An organization's plan, subscription, limits and usage")
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def org_billing(request, org_id):
    org = _org_admin(request, org_id)
    sub = Subscription.objects.filter(organization=org).select_related("plan").first()
    return Response(
        {
            "billing_enabled": service.enabled(),
            "plan": PublicPlanSerializer(sub.plan).data if sub and sub.plan else None,
            "status": sub.status if sub else "",
            "current_period_end": sub.current_period_end if sub else None,
            "cancel_at_period_end": sub.cancel_at_period_end if sub else False,
            "has_billing_account": bool(sub and sub.stripe_customer_id),
            "limits": {
                k: getattr(org, k) for k in ("max_workspaces", "max_channels", "max_members", "ai_monthly_tokens")
            },
            "usage": limits.usage(org),
        }
    )


@doc(
    "billing", "Start a Stripe Checkout", description='Body: {"plan": <id>}. Redirect the browser to the returned url.'
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def checkout(request, org_id):
    org = _org_admin(request, org_id)
    plan = Plan.objects.filter(pk=request.data.get("plan"), active=True).first()
    if plan is None:
        raise ValidationError({"plan": "Unknown plan."})
    try:
        return Response({"url": service.checkout_url(org, plan, request.user)})
    except stripe.StripeError as exc:
        raise ValidationError({"detail": f"Stripe error: {exc.user_message or exc.__class__.__name__}"})


@doc("billing", "Open the Stripe customer portal")
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def portal(request, org_id):
    org = _org_admin(request, org_id)
    try:
        return Response({"url": service.portal_url(org)})
    except stripe.StripeError as exc:
        raise ValidationError({"detail": f"Stripe error: {exc.user_message or exc.__class__.__name__}"})


@hidden
@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def webhook(request):
    try:
        event = service.construct_event(request.body, request.headers.get("Stripe-Signature", ""))
    except (ValueError, stripe.SignatureVerificationError):
        return Response({"detail": "Invalid payload or signature."}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"result": service.handle_event(event)})


class PlatformPlanViewSet(viewsets.ModelViewSet):
    permission_classes = [IsSuperAdmin]
    serializer_class = PlanSerializer
    queryset = Plan.objects.all()
    pagination_class = None

    def perform_destroy(self, plan):
        if plan.subscriptions.exists():
            raise ValidationError({"detail": "Organizations are subscribed to this plan; deactivate it instead."})
        plan.delete()

    @action(detail=True, methods=["post"], url_path="stripe-price")
    def stripe_price(self, request, pk=None):
        """Create the Stripe product and recurring price for this plan."""
        plan = self.get_object()
        try:
            amount = int(request.data.get("amount_cents"))
            if amount <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValidationError({"amount_cents": "A positive price in cents, e.g. 2900."})
        currency = (request.data.get("currency") or "eur").lower()
        interval = request.data.get("interval") or "month"
        if interval not in ("month", "year"):
            raise ValidationError({"interval": "month or year."})
        try:
            service.create_stripe_price(plan, amount, currency, interval)
        except stripe.StripeError as exc:
            raise ValidationError({"detail": f"Stripe error: {exc.user_message or exc.__class__.__name__}"})
        return Response(PlanSerializer(plan).data)

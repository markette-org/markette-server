from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.tenants.models import Role
from apps.tenants.tenancy import WorkspaceScopedMixin

from . import senders
from .events import app_url, workspace_data
from .models import DEFAULT_EVENTS, Delivery, Destination, Event, Kind, new_signing_secret
from .serializers import DeliverySerializer, DestinationSerializer
from .tasks import attempt


class DestinationViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    """Where the workspace's notifications go. Admins only: destinations hold addresses and tokens."""

    queryset = Destination.objects.all()
    serializer_class = DestinationSerializer
    required_role = Role.ADMIN
    pagination_class = None

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        dest = Destination.objects.get(pk=response.data["id"])
        if dest.kind == Kind.WEBHOOK:
            # Shown once, like an API token: the receiver needs it to verify signatures.
            response.data["signing_secret"] = dest.secrets["signing_secret"]
        return response

    @action(detail=False, methods=["get"])
    def catalog(self, request):
        return Response(
            {
                "events": [{"value": e.value, "label": e.label} for e in Event],
                "default_events": [str(e) for e in DEFAULT_EVENTS],
                "kinds": [{"value": k.value, "label": k.label} for k in Kind],
                "email_configured": senders.email_configured(),
            }
        )

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        """Sends a test notification right away and reports what happened."""
        dest = self.get_object()
        payload = {
            "event": "test",
            "created_at": timezone.now().isoformat(),
            "workspace": workspace_data(self.workspace),
            "data": {
                "message": f"Test notification from Markette for {self.workspace.name}",
                "post": {"url": app_url("/settings#notifications")},
            },
        }
        d = Delivery.objects.create(destination=dest, event="test", payload=payload, attempts=1)
        attempt(d)
        d.refresh_from_db()
        return Response(DeliverySerializer(d).data)

    @action(detail=True, methods=["get"])
    def deliveries(self, request, pk=None):
        dest = self.get_object()
        return Response(DeliverySerializer(dest.deliveries.all()[:50], many=True).data)

    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request, pk=None):
        dest = self.get_object()
        if dest.kind != Kind.WEBHOOK:
            raise ValidationError("Only webhooks have a signing secret.")
        dest.secrets = {**dest.secrets, "signing_secret": new_signing_secret()}
        dest.save(update_fields=["secrets", "updated_at"])
        return Response({"signing_secret": dest.secrets["signing_secret"]})

    @action(detail=False, methods=["post"], url_path="telegram-chats")
    def telegram_chats(self, request):
        """Lists the chats a bot can post to: send it a message (or add it to a group/channel) first."""
        token = (request.data.get("bot_token") or "").strip()
        if not token and request.data.get("destination"):
            dest = self.get_queryset().filter(pk=request.data["destination"], kind=Kind.TELEGRAM).first()
            token = (dest.secrets or {}).get("bot_token", "") if dest else ""
        if not token:
            raise ValidationError({"bot_token": "Paste the token BotFather gave you."})
        try:
            found = senders.telegram_chats(token)
        except senders.DeliveryError as exc:
            if exc.status == 409:
                raise ValidationError(
                    {
                        "bot_token": "This bot has a webhook set elsewhere, so its chats cannot be listed. "
                        "Enter the chat id by hand."
                    }
                ) from exc
            raise ValidationError({"bot_token": str(exc)}) from exc
        return Response(found, status=status.HTTP_200_OK)

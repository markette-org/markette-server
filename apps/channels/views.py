import logging
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import get_object_or_404, redirect
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.core.docs import doc, hidden
from apps.tenants.models import Role
from apps.tenants.tenancy import WorkspaceScopedMixin

from .models import Channel, ConnectionRequest
from .providers import REGISTRY, ProviderError, get_provider
from .serializers import ChannelSerializer, ConnectSerializer, SelectCandidatesSerializer
from .services import candidate_to_dict, create_channels, public_candidate, set_relevance

log = logging.getLogger(__name__)


@doc(
    "providers",
    "Supported networks and sites",
    description="Capabilities (character limits, media, first comment, metrics) and the fields needed to connect each provider.",
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def providers(request):
    return Response([p.describe() for p in REGISTRY.values()])


class ChannelViewSet(
    WorkspaceScopedMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Channel.objects.prefetch_related("teams", "workspaces")
    serializer_class = ChannelSerializer
    filterset_fields = ("provider", "status")

    def get_queryset(self):
        """Every channel of the organization, the ones relevant to this workspace first."""
        from django.db.models import Exists, OuterRef

        from apps.teams.access import filter_channels

        from .models import ChannelAccess

        ws = self.workspace
        qs = Channel.objects.prefetch_related("teams", "workspaces").filter(organization_id=ws.organization_id)
        qs = filter_channels(qs, self.membership)
        qs = qs.annotate(is_relevant=Exists(ChannelAccess.objects.filter(channel=OuterRef("pk"), workspace=ws)))
        if team := self.request.query_params.get("team"):
            qs = qs.filter(teams=team)
        if self.request.query_params.get("relevant") == "1":
            qs = qs.filter(is_relevant=True)
        return qs.order_by("-is_relevant", "provider", "name")

    def perform_destroy(self, channel):
        """Disconnecting removes the account for the whole organization, so it is an org-admin action."""
        from apps.tenants.models import OrgMembership

        org_admin = OrgMembership.objects.filter(
            organization_id=channel.organization_id, user=self.request.user, role__in=("owner", "admin")
        ).exists()
        if not org_admin:
            raise PermissionDenied(
                "Channels belong to the organization; ask an organization admin to disconnect it, "
                "or mark it as not relevant for this workspace."
            )
        channel.delete()

    @action(detail=True, methods=["post"])
    def relevance(self, request, pk=None):
        """Mark the channel as relevant (shown first, preselected) for this workspace, or not."""
        channel = self.get_object()
        set_relevance(channel, self.workspace, bool(request.data.get("relevant")))
        channel = self.get_queryset().get(pk=channel.pk)
        return Response(ChannelSerializer(channel, context=self.get_serializer_context()).data)

    def get_permissions(self):
        if self.action in ("destroy", "connect", "select", "relevance", "add_subreddit"):
            self.required_role = Role.ADMIN
        return super().get_permissions()

    @action(detail=False, methods=["post"])
    def connect(self, request):
        s = ConnectSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        key = s.validated_data["provider"]
        if key not in REGISTRY:
            raise ValidationError({"provider": "Unknown provider."})
        cls = get_provider(key)
        if not cls.is_configured():
            raise ValidationError(
                {"provider": f"{cls.label} is not configured on this server (missing app credentials)."}
            )
        conn = ConnectionRequest.objects.create(
            workspace=self.workspace,
            user=request.user,
            provider=key,
            base_url=s.validated_data.get("base_url", ""),
        )
        provider = cls()
        try:
            if cls.capabilities.auth == "oauth2":
                return Response({"request_id": conn.id, "authorize_url": provider.authorization_url(conn)})
            creds = s.validated_data.get("credentials") or {}
            missing = [f.name for f in cls.credential_fields if f.required and not creds.get(f.name)]
            if missing:
                raise ValidationError({"credentials": f"Missing: {', '.join(missing)}"})
            candidates = provider.connect_with_credentials(creds)
        except ProviderError as exc:
            conn.error = str(exc)
            conn.save(update_fields=["error"])
            detail = {"detail": str(exc)}
            if isinstance(exc.payload, dict) and exc.payload.get("callback_url"):
                detail["callback_url"] = exc.payload["callback_url"]
            raise ValidationError(detail) from exc
        conn.candidates = {"items": [candidate_to_dict(c) for c in candidates]}
        conn.save(update_fields=["candidates"])
        # Credential-based providers yield exactly the account that was entered.
        if len(candidates) == 1:
            channels = create_channels(conn, [candidates[0].external_id], request.user)
            return Response(
                {"request_id": conn.id, "channels": ChannelSerializer(channels, many=True).data},
                status=status.HTTP_201_CREATED,
            )
        return Response({"request_id": conn.id, "candidates": [public_candidate(c) for c in conn.candidates["items"]]})

    @action(detail=False, methods=["get", "post"], url_path=r"connections/(?P<request_id>[0-9a-f-]+)")
    def select(self, request, request_id=None):
        conn = get_object_or_404(ConnectionRequest, pk=request_id, workspace=self.workspace, user=request.user)
        if request.method == "GET":
            return Response(
                {
                    "request_id": conn.id,
                    "provider": conn.provider,
                    "error": conn.error,
                    "completed": conn.completed_at is not None,
                    "expired": conn.expired,
                    "candidates": [public_candidate(c) for c in conn.candidates.get("items", [])],
                }
            )
        if conn.expired or conn.completed_at:
            raise ValidationError({"detail": "This connection attempt has expired; start again."})
        s = SelectCandidatesSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        channels = create_channels(conn, s.validated_data["external_ids"], request.user)
        return Response(ChannelSerializer(channels, many=True).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"])
    def subscribers(self, request, pk=None):
        """WhatsApp broadcast list. POST {address, name, consent: true}, or {lines: "phone,name\n…", consent: true}."""
        from django.utils import timezone

        from .models import Subscriber
        from .providers.whatsapp import normalize_phone

        channel = self.get_object()
        if channel.provider != "whatsapp":
            raise ValidationError({"detail": "Only WhatsApp channels have subscriber lists."})
        if not self.membership.has_role(Role.ADMIN):
            raise PermissionDenied("Only workspace admins can see or manage subscribers (personal data).")
        if request.method == "GET":
            qs = channel.subscribers.all()
            if request.query_params.get("active") == "1":
                qs = qs.filter(opted_out_at__isnull=True)
            if q := request.query_params.get("search"):
                qs = qs.filter(address__contains=q) | qs.filter(name__icontains=q)
            page = self.paginate_queryset(qs)
            rows = [
                {
                    "id": s.id,
                    "address": s.address,
                    "name": s.name,
                    "source": s.source,
                    "opted_in_at": s.opted_in_at,
                    "opted_out_at": s.opted_out_at,
                }
                for s in page
            ]
            return self.get_paginated_response(rows)
        if not self.membership.has_role(Role.ADMIN):
            raise PermissionDenied("Only workspace admins manage subscribers.")
        if request.data.get("consent") is not True:
            raise ValidationError({"consent": "Confirm these people agreed to receive your WhatsApp messages."})
        lines = request.data.get("lines")
        entries = (
            [(request.data.get("address", ""), request.data.get("name", ""))]
            if lines is None
            else [
                tuple(part.strip() for part in (line.split(",", 1) + [""])[:2])
                for line in str(lines).splitlines()
                if line.strip()
            ]
        )
        added, invalid = 0, []
        for address, name in entries:
            try:
                phone = normalize_phone(address)
            except ValueError:
                invalid.append(address)
                continue
            source = "manual" if lines is None else "import"
            _, created = Subscriber.objects.update_or_create(
                channel=channel,
                address=phone,
                defaults={"name": name[:200], "source": source, "opted_in_at": timezone.now(), "opted_out_at": None},
            )
            added += 1
        if lines is None and invalid:
            raise ValidationError({"address": "Use the international format, e.g. +39 333 1234567."})
        return Response({"added": added, "invalid": invalid}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["delete"], url_path=r"subscribers/(?P<subscriber_id>\d+)")
    def remove_subscriber(self, request, pk=None, subscriber_id=None):
        """Opt someone out (kept for the record, never messaged again)."""
        from django.utils import timezone

        channel = self.get_object()
        if not self.membership.has_role(Role.ADMIN):
            raise PermissionDenied("Only workspace admins manage subscribers.")
        channel.subscribers.filter(pk=subscriber_id).update(opted_out_at=timezone.now())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request, pk=None):
        """New signing secret for a webhook channel; the old one stops working immediately."""
        import secrets as _secrets

        channel = self.get_object()
        if channel.provider != "webhook":
            raise ValidationError({"detail": "Only webhook channels have a signing secret."})
        if not self.membership.has_role(Role.ADMIN):
            raise PermissionDenied("Only workspace admins can rotate the signing secret.")
        channel.credentials = {**channel.credentials, "secret": _secrets.token_hex(24)}
        channel.save(update_fields=["credentials", "updated_at"])
        return Response(ChannelSerializer(channel, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["get"])
    def flairs(self, request, pk=None):
        """Post flairs available in a Reddit channel's subreddit."""
        channel = self.get_object()
        if channel.provider != "reddit":
            raise ValidationError({"detail": "Only Reddit channels have post flairs."})
        provider = channel.get_provider()
        try:
            provider.refresh_credentials()
            return Response(provider.flairs())
        except ProviderError as exc:
            raise _reddit_error(channel, exc) from exc

    @action(detail=True, methods=["post"], url_path="add-subreddit")
    def add_subreddit(self, request, pk=None):
        """Add a specific subreddit as a channel, using this Reddit channel's account. Body: {name}."""
        from .services import create_channels

        channel = self.get_object()
        if channel.provider != "reddit":
            raise ValidationError({"detail": "Subreddits can only be added from a Reddit channel."})
        provider = channel.get_provider()
        try:
            provider.refresh_credentials()
            candidate = provider.subreddit_candidate(str(request.data.get("name") or ""))
        except ProviderError as exc:
            raise _reddit_error(channel, exc) from exc
        conn = ConnectionRequest.objects.create(
            workspace=self.workspace,
            user=request.user,
            provider="reddit",
            candidates={"items": [candidate_to_dict(candidate)]},
        )
        [created] = create_channels(conn, [candidate.external_id], request.user)
        return Response(
            ChannelSerializer(created, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def sync(self, request, pk=None):
        """Re-validate credentials and refresh the audience size."""
        channel = self.get_object()
        provider = channel.get_provider()
        try:
            provider.refresh_credentials()
            followers = provider.fetch_audience()
        except ProviderError as exc:
            if exc.auth:
                channel.mark_needs_reauth(str(exc))
            raise ValidationError({"detail": str(exc)}) from exc
        if followers is not None:
            channel.followers_count = followers
        channel.status = "active"
        channel.last_error = ""
        channel.save()
        return Response(ChannelSerializer(channel).data)


@hidden
@api_view(["GET"])
@permission_classes([AllowAny])
def oauth_callback(request, provider):
    """Landing point for every OAuth redirect; bounces the browser back to the app."""
    state = request.GET.get("state", "")
    conn = get_object_or_404(ConnectionRequest, state=state, provider__in=_provider_aliases(provider))
    target = f"{settings.FRONTEND_URL}/channels/connect/{conn.id}"
    if conn.expired or conn.completed_at:
        return redirect(f"{target}?{urlencode({'error': 'expired'})}")
    if request.GET.get("error"):
        conn.error = request.GET.get("error_description") or request.GET["error"]
        conn.save(update_fields=["error"])
        return redirect(f"{target}?{urlencode({'error': conn.error})}")
    try:
        candidates = get_provider(conn.provider)().complete_oauth(conn, request.GET)
    except (ProviderError, KeyError) as exc:
        log.warning("OAuth completion failed for %s: %s", provider, exc)
        conn.error = str(exc)
        conn.save(update_fields=["error"])
        return redirect(f"{target}?{urlencode({'error': str(exc)[:300]})}")
    conn.candidates = {"items": [candidate_to_dict(c) for c in candidates]}
    conn.save(update_fields=["candidates"])
    # A single account needs no picker.
    if len(candidates) == 1:
        create_channels(conn, [candidates[0].external_id], conn.user)
        return redirect(f"{settings.FRONTEND_URL}/channels?{urlencode({'connected': conn.provider})}")
    return redirect(target)


def _reddit_error(channel, exc):
    if exc.auth:
        channel.mark_needs_reauth(str(exc))
        return ValidationError({"detail": "Reddit no longer accepts this channel's access; reconnect Reddit."})
    return ValidationError({"detail": str(exc)})


def _provider_aliases(provider):
    # Facebook and Instagram share the Meta redirect URI.
    return ["facebook", "instagram"] if provider in ("facebook", "instagram") else [provider]

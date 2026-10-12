from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.analytics.views import AnalyticsViewSet
from apps.channels import webhooks as channel_webhooks
from apps.channels.views import ChannelViewSet, oauth_callback, providers
from apps.core import views as core_views
from apps.insights.views import InsightsViewSet
from apps.notifications.views import DestinationViewSet
from apps.publishing.views import (
    CalendarViewSet,
    CampaignViewSet,
    MediaViewSet,
    PostViewSet,
    SlotViewSet,
    TargetViewSet,
)
from apps.sso.views import PasswordTokenView
from apps.teams.views import TeamViewSet
from apps.tenants.views import (
    InvitationViewSet,
    MemberViewSet,
    OrganizationViewSet,
    WorkspaceViewSet,
    accept_invitation,
    me,
    register,
)

router = DefaultRouter()
router.register("organizations", OrganizationViewSet, basename="organization")
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("teams", TeamViewSet, basename="team")
router.register("members", MemberViewSet, basename="member")
router.register("invitations", InvitationViewSet, basename="invitation")
router.register("channels", ChannelViewSet, basename="channel")
router.register("media", MediaViewSet, basename="media")
router.register("campaigns", CampaignViewSet, basename="campaign")
router.register("slots", SlotViewSet, basename="slot")
router.register("posts", PostViewSet, basename="post")
router.register("targets", TargetViewSet, basename="target")
router.register("calendar", CalendarViewSet, basename="calendar")
router.register("analytics", AnalyticsViewSet, basename="analytics")
router.register("insights", InsightsViewSet, basename="insights")
router.register("notifications", DestinationViewSet, basename="notification")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/register/", register),
    path("api/auth/token/", PasswordTokenView.as_view()),
    path("api/auth/token/refresh/", TokenRefreshView.as_view()),
    path("api/auth/me/", me),
    path("api/auth/tokens/", core_views.tokens),
    path("api/auth/tokens/<int:token_id>/", core_views.revoke_token),
    path("api/invitations/accept/", accept_invitation),
    path("api/providers/", providers),
    path("api/oauth/<str:provider>/callback", oauth_callback),
    path("api/channels/whatsapp/webhook/", channel_webhooks.whatsapp_webhook),
    path("api/channels/qq/webhook/<str:app_id>/", channel_webhooks.qq_webhook),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    path("api/platform/", include("apps.platform.urls")),
    path("api/billing/", include("apps.billing.urls")),
    path("api/", include("apps.sso.urls")),
    path("api/", include(router.urls)),
    # Media must be publicly reachable: Instagram and Facebook fetch it by URL.
    # Serve it from the CDN / reverse proxy in production for performance.
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]

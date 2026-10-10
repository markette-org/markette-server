from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.billing.views import PlatformPlanViewSet

from . import settings_api, views

router = DefaultRouter()
router.register("organizations", views.PlatformOrganizationViewSet, basename="platform-organization")
router.register("users", views.PlatformUserViewSet, basename="platform-user")
router.register("plans", PlatformPlanViewSet, basename="platform-plan")

urlpatterns = [
    path("overview/", views.overview),
    path("health/", views.health),
    path("ai-usage/", views.ai_usage),
    path("settings/", settings_api.platform_settings),
    path("settings/test-ai/", settings_api.test_ai),
    path("public/", settings_api.public_settings),
    path("", include(router.urls)),
]

from django.urls import path

from . import views

urlpatterns = [
    path("plans/", views.plans),
    path("webhook/", views.webhook),
    path("organizations/<int:org_id>/", views.org_billing),
    path("organizations/<int:org_id>/checkout/", views.checkout),
    path("organizations/<int:org_id>/portal/", views.portal),
]

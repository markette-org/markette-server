from django.urls import path

from . import views

urlpatterns = [
    path("auth/sso/discover/", views.discover),
    path("auth/sso/start/<slug:org_slug>/", views.start),
    path("auth/sso/callback/", views.callback),
    path("auth/sso/exchange/", views.exchange),
    path("organizations/<int:org_id>/sso/", views.config),
    path("organizations/<int:org_id>/sso/domains/", views.add_domain),
    path("organizations/<int:org_id>/sso/domains/<int:domain_id>/verify/", views.verify_domain),
    path("organizations/<int:org_id>/sso/domains/<int:domain_id>/", views.remove_domain),
]

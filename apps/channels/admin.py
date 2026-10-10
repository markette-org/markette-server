from django.contrib import admin

from .models import Channel, ConnectionRequest, OAuthApp


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ("name", "provider", "organization", "status", "followers_count", "token_expires_at")
    list_filter = ("provider", "status")
    search_fields = ("name", "handle", "external_id")
    exclude = ("credentials",)


@admin.register(OAuthApp)
class OAuthAppAdmin(admin.ModelAdmin):
    list_display = ("provider", "base_url", "client_id")
    exclude = ("secrets",)


@admin.register(ConnectionRequest)
class ConnectionRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "provider", "workspace", "user", "completed_at", "created_at")
    exclude = ("secrets", "candidates")

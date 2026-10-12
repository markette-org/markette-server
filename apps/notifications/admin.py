from django.contrib import admin

from .models import Delivery, Destination


@admin.register(Destination)
class DestinationAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "workspace", "enabled", "failures", "last_sent_at")
    list_filter = ("kind", "enabled")
    exclude = ("secrets",)


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ("uid", "event", "destination", "status", "attempts", "created_at")
    list_filter = ("status", "event")

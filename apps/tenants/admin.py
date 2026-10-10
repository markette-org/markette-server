from django.contrib import admin

from .models import Invitation, Membership, Workspace


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    raw_id_fields = ("user",)


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "created_at")
    search_fields = ("name", "slug")
    inlines = [MembershipInline]


admin.site.register(Invitation)

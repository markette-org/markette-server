from django.contrib import admin

from .models import SSOConfig, SSODomain


class DomainInline(admin.TabularInline):
    model = SSODomain
    extra = 0
    readonly_fields = ("verification_token", "verified_at")


@admin.register(SSOConfig)
class SSOConfigAdmin(admin.ModelAdmin):
    list_display = ("organization", "enabled", "issuer", "enforce", "auto_provision")
    exclude = ("secrets",)
    inlines = [DomainInline]

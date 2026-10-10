from django.contrib import admin

from .models import Team, TeamMember


class TeamMemberInline(admin.TabularInline):
    model = TeamMember
    extra = 0
    raw_id_fields = ("user",)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "requires_approval")
    filter_horizontal = ("channels",)
    inlines = [TeamMemberInline]

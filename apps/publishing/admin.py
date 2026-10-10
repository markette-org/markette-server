from django.contrib import admin

from .models import Campaign, MediaAsset, Post, PostingSlot, PostTarget


class TargetInline(admin.TabularInline):
    model = PostTarget
    extra = 0
    fields = ("channel", "status", "scheduled_at", "published_at", "external_url", "attempts", "last_error")
    readonly_fields = ("published_at", "external_url", "attempts", "last_error")


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "workspace", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("title", "body")
    inlines = [TargetInline]


@admin.register(PostTarget)
class PostTargetAdmin(admin.ModelAdmin):
    list_display = ("id", "post", "channel", "status", "scheduled_at", "published_at", "attempts")
    list_filter = ("status", "channel__provider")


admin.site.register(MediaAsset)
admin.site.register(Campaign)
admin.site.register(PostingSlot)

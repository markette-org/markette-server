from django.contrib import admin

from .models import AIRequest, BestTimeProfile


@admin.register(AIRequest)
class AIRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "workspace", "kind", "model", "input_tokens", "output_tokens", "stop_reason")
    list_filter = ("kind", "model")


admin.site.register(BestTimeProfile)

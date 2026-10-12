import secrets

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.decorators import action, api_view, permission_classes, throttle_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from apps.core.docs import doc
from apps.notifications.senders import DeliveryError
from apps.tenants.models import Role
from apps.tenants.tenancy import WorkspaceScopedMixin

from . import reports
from .models import Report


class ReportSerializer(serializers.ModelSerializer):
    share_url = serializers.SerializerMethodField()
    recipients = serializers.ListField(child=serializers.EmailField(), required=False, max_length=20)

    class Meta:
        model = Report
        fields = (
            "id",
            "name",
            "period",
            "frequency",
            "recipients",
            "campaign",
            "channels",
            "share_url",
            "last_sent_at",
            "last_error",
            "created_at",
        )
        read_only_fields = ("last_sent_at", "last_error", "created_at")

    def get_share_url(self, obj) -> str | None:
        from django.conf import settings

        return f"{settings.FRONTEND_URL}/report/{obj.share_token}" if obj.share_token else None

    def validate_campaign(self, value):
        ws = self.context.get("workspace")
        if value and ws and value.workspace_id != ws.pk:
            raise serializers.ValidationError("Unknown campaign.")
        return value

    def validate_channels(self, value):
        if not isinstance(value, list) or not all(isinstance(v, int) for v in value):
            raise serializers.ValidationError("A list of channel ids.")
        return value

    def validate(self, attrs):
        freq = attrs.get("frequency", self.instance.frequency if self.instance else "")
        recipients = attrs.get("recipients", self.instance.recipients if self.instance else [])
        if freq and not recipients:
            raise serializers.ValidationError({"recipients": "Add at least one address to email it."})
        return attrs


class ReportViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    """Saved reports: emailed weekly or monthly with a CSV, and/or shared as a read-only page (admins)."""

    queryset = Report.objects.all()
    serializer_class = ReportSerializer
    required_role = Role.ADMIN
    pagination_class = None

    def perform_create(self, serializer):
        serializer.save(workspace=self.workspace, created_by=self.request.user)

    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        return Response(reports.data(self.get_object()))

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        report = self.get_object()
        try:
            reports.send(report)
        except DeliveryError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(ReportSerializer(report, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post", "delete"])
    def share(self, request, pk=None):
        """POST creates (or replaces) the public link; DELETE withdraws it."""
        report = self.get_object()
        report.share_token = secrets.token_urlsafe(24) if request.method == "POST" else None
        report.save(update_fields=["share_token"])
        return Response(ReportSerializer(report, context=self.get_serializer_context()).data)


class PublicThrottle(AnonRateThrottle):
    rate = "120/hour"


@doc("analytics", "A shared report (no account)")
@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([PublicThrottle])
def public_report(request, token):
    report = get_object_or_404(Report.objects.select_related("workspace"), share_token=token)
    if not report.workspace.is_live:
        return Response({"detail": "This report is no longer available."}, status=404)
    if request.query_params.get("download") == "csv":
        d0, d1 = reports.period_range(report, timezone.now().astimezone(report.workspace.tz).date())
        resp = HttpResponse(reports.csv_bytes(report, d0, d1), content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="markette-{d0}-{d1}.csv"'
        return resp
    data = reports.data(report)
    # Shared pages show what was published, not internal ids.
    for p in data["top_posts"]:
        p.pop("target", None)
        p.pop("post", None)
    return Response(data)

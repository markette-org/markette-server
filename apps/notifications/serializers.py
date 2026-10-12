import re

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from rest_framework import serializers

from .models import DEFAULT_EVENTS, Delivery, Destination, Event, Kind, new_signing_secret
from .senders import email_configured

URL_RE = re.compile(r"^https?://[^\s/]+\S*$")
CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{4,64})$")


class DestinationSerializer(serializers.ModelSerializer):
    events = serializers.ListField(child=serializers.ChoiceField(choices=Event.choices), required=False)
    # Write-only secret: the bot token is stored encrypted and never returned.
    bot_token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    has_bot_token = serializers.SerializerMethodField()

    class Meta:
        model = Destination
        fields = (
            "id",
            "kind",
            "name",
            "events",
            "enabled",
            "config",
            "bot_token",
            "has_bot_token",
            "last_sent_at",
            "last_error",
            "failures",
            "created_at",
        )
        read_only_fields = ("last_sent_at", "last_error", "failures", "created_at")

    def get_has_bot_token(self, obj) -> bool:
        return bool((obj.secrets or {}).get("bot_token"))

    def validate(self, attrs):
        kind = attrs.get("kind") or (self.instance.kind if self.instance else None)
        if self.instance and "kind" in attrs and attrs["kind"] != self.instance.kind:
            raise serializers.ValidationError({"kind": "Create a new destination to change its kind."})
        config = attrs.get("config", self.instance.config if self.instance else {}) or {}
        if not isinstance(config, dict):
            raise serializers.ValidationError({"config": "Must be an object."})
        if kind == Kind.EMAIL:
            if not email_configured():
                raise serializers.ValidationError({"kind": "This server has no email settings (EMAIL_HOST)."})
            to = [str(a).strip() for a in config.get("to") or [] if str(a).strip()]
            if len(to) > 20:
                raise serializers.ValidationError({"config": "At most 20 addresses."})
            for a in to:
                try:
                    validate_email(a)
                except DjangoValidationError as exc:
                    raise serializers.ValidationError({"config": f"Not an email address: {a}"}) from exc
            config = {"to": to}
        elif kind == Kind.TELEGRAM:
            chat = str(config.get("chat_id") or "").strip()
            if not CHAT_RE.match(chat):
                raise serializers.ValidationError(
                    {"config": "Choose the chat: a numeric chat id, or @channelname for a public channel."}
                )
            config = {
                "chat_id": int(chat) if chat.lstrip("-").isdigit() else chat,
                "chat_title": str(config.get("chat_title") or "")[:120],
            }
            token = (attrs.get("bot_token") or "").strip()
            if not token and not (self.instance and self.instance.secrets.get("bot_token")):
                raise serializers.ValidationError({"bot_token": "Paste the token BotFather gave you."})
            if token and not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
                raise serializers.ValidationError({"bot_token": "That does not look like a bot token (123456:ABC…)."})
        elif kind == Kind.WEBHOOK:
            url = str(config.get("url") or "").strip()
            if not URL_RE.match(url):
                raise serializers.ValidationError({"config": "Enter the URL to call, starting with https://."})
            config = {"url": url[:1000]}
        attrs["config"] = config
        if not self.instance and not attrs.get("events"):
            attrs["events"] = [str(e) for e in DEFAULT_EVENTS]
        if "events" in attrs and not attrs["events"]:
            raise serializers.ValidationError({"events": "Choose at least one event."})
        if "events" in attrs:
            attrs["events"] = sorted(set(attrs["events"]))
        return attrs

    def _secrets(self, instance, validated):
        token = (validated.pop("bot_token", "") or "").strip()
        secrets = dict(instance.secrets or {}) if instance else {}
        if token:
            secrets["bot_token"] = token
        return secrets

    def create(self, validated):
        secrets = self._secrets(None, validated)
        if validated.get("kind") == Kind.WEBHOOK:
            secrets["signing_secret"] = new_signing_secret()
        return Destination.objects.create(secrets=secrets, **validated)

    def update(self, instance, validated):
        instance.secrets = self._secrets(instance, validated)
        if validated.get("enabled") and not instance.enabled:
            instance.failures = 0  # switched back on: give it a fresh start
        return super().update(instance, validated)


class DeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = Delivery
        fields = (
            "uid",
            "event",
            "status",
            "attempts",
            "response_status",
            "error",
            "created_at",
            "sent_at",
            "next_attempt_at",
            "payload",
        )

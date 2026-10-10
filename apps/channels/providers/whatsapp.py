"""WhatsApp broadcasts through the WhatsApp Business Platform (Cloud API).

WhatsApp has no API for posting to WhatsApp Channels, and groups created through
the Groups API are limited to 8 participants. The supported way to broadcast news
is to send an **approved template message** to people who **opted in**. Markette
keeps that subscriber list per channel (people opt in by messaging JOIN, opt out
with STOP) and tracks each message's delivery through the Cloud API webhook.

Setup in Meta: a WhatsApp Business Account, a phone number, a system-user access
token with whatsapp_business_messaging/management, and a MARKETING template whose
body has one variable ({{1}}) that receives the post text. An IMAGE header is used
when the post has an image.
"""

import logging
import re
import time

from django.conf import settings
from django.utils import timezone

from .base import BaseProvider, Candidate, Capabilities, CredentialField, Metrics, ProviderError, PublishResult

log = logging.getLogger(__name__)
TEMPLATE_PARAM_LIMIT = 1024
SEND_PAUSE = 1 / 40  # stay well under the default 80 messages/second


def graph():
    return f"https://graph.facebook.com/{settings.META_GRAPH_VERSION}"


def verify_token(phone_id):
    """Deterministic per-number token for Meta's webhook verification handshake."""
    import hashlib
    import hmac

    return hmac.new(settings.SECRET_KEY.encode(), f"whatsapp:{phone_id}".encode(), hashlib.sha256).hexdigest()[:32]


def webhook_url():
    return f"{settings.PUBLIC_API_URL}/api/channels/whatsapp/webhook/"


def normalize_phone(value):
    digits = re.sub(r"[^\d]", "", value or "")
    if len(digits) < 8 or len(digits) > 15:
        raise ValueError(f"Not an international phone number: {value!r}")
    return digits


class WhatsAppProvider(BaseProvider):
    key = "whatsapp"
    label = "WhatsApp"
    capabilities = Capabilities(
        max_chars=TEMPLATE_PARAM_LIMIT,
        max_media=1,
        supports_video=False,
        auth="credentials",
        metric_names=("impressions", "reach"),
    )
    credential_fields = [
        CredentialField("phone_number_id", "Phone number ID", help="WhatsApp Manager → API setup."),
        CredentialField("waba_id", "WhatsApp Business Account ID"),
        CredentialField(
            "access_token",
            "Access token",
            type="password",
            help="A permanent system-user token with whatsapp_business_messaging and whatsapp_business_management.",
        ),
        CredentialField(
            "template_name",
            "Broadcast template",
            help="An approved MARKETING template whose body is {{1}} (the post text). Optional IMAGE header.",
        ),
        CredentialField("template_language", "Template language", default="en", required=False),
    ]

    def _h(self, token=None):
        return {"Authorization": f"Bearer {token or self.creds['access_token']}"}

    def error_from_response(self, resp):
        err = super().error_from_response(resp)
        try:
            e = resp.json().get("error", {})
        except ValueError:
            return err
        if e.get("code") == 190:
            err.auth = True
        elif e.get("code") in (4, 80007, 130429, 131048, 131056):  # rate / pair rate limits
            err.retryable, err.retry_after = True, err.retry_after or 60
        return err

    # --- connection -------------------------------------------------------------
    def connect_with_credentials(self, data):
        token = data["access_token"].strip()
        phone_id = data["phone_number_id"].strip()
        waba = data["waba_id"].strip()
        number = self.http(
            "GET",
            f"{graph()}/{phone_id}",
            headers=self._h(token),
            params={"fields": "display_phone_number,verified_name,quality_rating"},
        ).json()
        template = self._find_template(
            token, waba, data["template_name"].strip(), data.get("template_language") or "en"
        )
        return [
            Candidate(
                external_id=phone_id,
                profile_url=f"https://wa.me/{normalize_phone(number.get('display_phone_number', ''))}"
                if number.get("display_phone_number")
                else "",
                name=number.get("verified_name") or number.get("display_phone_number", phone_id),
                handle=number.get("display_phone_number", ""),
                credentials={"access_token": token},
                config={
                    "waba_id": waba,
                    "template_name": template["name"],
                    "template_language": template["language"],
                    "template_has_image": template["has_image"],
                    "join_keyword": "JOIN",
                    "stop_keyword": "STOP",
                    "verify_token": verify_token(phone_id),
                },
            )
        ]

    def _find_template(self, token, waba, name, language):
        rows = (
            self.http(
                "GET", f"{graph()}/{waba}/message_templates", headers=self._h(token), params={"name": name, "limit": 50}
            )
            .json()
            .get("data", [])
        )
        match = next((t for t in rows if t["name"] == name and t.get("language") == language), None)
        if match is None:
            raise ProviderError(f"Template '{name}' ({language}) was not found in this WhatsApp Business Account.")
        if match.get("status") != "APPROVED":
            raise ProviderError(f"Template '{name}' is {match.get('status')}; it must be APPROVED by Meta.")
        body = next((c for c in match.get("components", []) if c.get("type") == "BODY"), {})
        if "{{1}}" not in body.get("text", ""):
            raise ProviderError(f"Template '{name}' needs a body variable {{{{1}}}} for the post text.")
        header = next((c for c in match.get("components", []) if c.get("type") == "HEADER"), {})
        return {"name": name, "language": language, "has_image": header.get("format") == "IMAGE"}

    # --- publishing ---------------------------------------------------------------
    def validate(self, content):
        problems = super().validate(content)
        if self.channel is not None and content.media and not self.channel.config.get("template_has_image"):
            problems.append("The WhatsApp template has no image header; remove the image or use another template.")
        if "\n\n\n" in content.text or "    " in content.text:
            problems.append("WhatsApp templates refuse more than two line breaks or four spaces in a row.")
        return problems

    def _components(self, content):
        text = content.text.strip()
        components = [{"type": "body", "parameters": [{"type": "text", "text": text}]}]
        if content.media and self.channel.config.get("template_has_image"):
            components.insert(
                0, {"type": "header", "parameters": [{"type": "image", "image": {"link": content.media[0].url}}]}
            )
        return components

    def publish(self, content):
        from apps.publishing.models import PostTarget

        from ..models import Delivery

        target_id = int(content.idempotency_key.rsplit("-", 1)[-1])
        target = PostTarget.objects.get(pk=target_id)
        already = set(Delivery.objects.filter(target=target).values_list("subscriber_id", flat=True))
        subscribers = self.channel.subscribers.filter(opted_out_at__isnull=True).exclude(pk__in=already)
        cfg = self.channel.config
        components = self._components(content)
        sent = failed = 0
        for sub in subscribers.iterator():
            body = {
                "messaging_product": "whatsapp",
                "to": sub.address,
                "type": "template",
                "template": {
                    "name": cfg["template_name"],
                    "language": {"code": cfg["template_language"]},
                    "components": components,
                },
            }
            try:
                resp = self.http("POST", f"{graph()}/{self.channel.external_id}/messages", headers=self._h(), json=body)
            except ProviderError as exc:
                if exc.auth or exc.retryable:
                    raise  # the dispatcher retries; subscribers already reached are skipped
                Delivery.objects.create(
                    target=target, subscriber=sub, message_id="", status="failed", error=str(exc)[:500]
                )
                failed += 1
                continue
            Delivery.objects.create(target=target, subscriber=sub, message_id=resp.json()["messages"][0]["id"])
            sent += 1
            time.sleep(SEND_PAUSE)
        if sent == 0 and failed == 0 and not already:
            raise ProviderError("This WhatsApp channel has no opted-in subscribers yet.")
        return PublishResult(external_id=f"broadcast-{target_id}", url="", extra={"sent": sent, "failed": failed})

    def fetch_metrics(self, external_ids):
        from django.db.models import Count, Q

        from ..models import Delivery

        out = {}
        for ext in external_ids:
            target_id = int(ext.rsplit("-", 1)[-1])
            agg = Delivery.objects.filter(target_id=target_id).aggregate(
                total=Count("id"),
                delivered=Count("id", filter=Q(status__in=("delivered", "read"))),
                read=Count("id", filter=Q(status="read")),
                failed=Count("id", filter=Q(status="failed")),
            )
            out[ext] = Metrics(impressions=agg["delivered"], reach=agg["read"], raw=agg)
        return out

    def fetch_audience(self):
        return self.channel.subscribers.filter(opted_out_at__isnull=True).count()


# --- webhook handling (called from views) ---------------------------------------------


def handle_webhook(payload):
    """Delivery statuses and JOIN/STOP keywords from the Cloud API webhook."""
    from ..models import Channel, Delivery, Subscriber

    handled = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            phone_id = (value.get("metadata") or {}).get("phone_number_id")
            for st in value.get("statuses", []) or []:
                status = st.get("status")
                if status not in ("sent", "delivered", "read", "failed"):
                    continue
                d = Delivery.objects.filter(message_id=st.get("id")).first()
                if d is None:
                    continue
                order = {"sent": 0, "delivered": 1, "read": 2, "failed": 3}
                if order[status] > order.get(d.status, 0) or status == "failed":
                    d.status = status
                    if status == "failed":
                        d.error = "; ".join(e.get("title", "") for e in st.get("errors", []))[:500]
                    d.save(update_fields=["status", "error", "updated_at"])
                handled += 1
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts", []) or []}
            for msg in value.get("messages", []) or []:
                text = ((msg.get("text") or {}).get("body") or "").strip().upper()
                for channel in Channel.objects.filter(provider="whatsapp", external_id=phone_id):
                    cfg = channel.config
                    sender = msg.get("from", "")
                    if text == cfg.get("join_keyword", "JOIN").upper():
                        Subscriber.objects.update_or_create(
                            channel=channel,
                            address=sender,
                            defaults={
                                "name": names.get(sender, "")[:200],
                                "source": "keyword",
                                "opted_in_at": timezone.now(),
                                "opted_out_at": None,
                            },
                        )
                        handled += 1
                    elif text in (cfg.get("stop_keyword", "STOP").upper(), "STOP", "UNSUBSCRIBE"):
                        Subscriber.objects.filter(channel=channel, address=sender).update(opted_out_at=timezone.now())
                        handled += 1
    return handled

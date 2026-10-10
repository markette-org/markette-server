"""Inbound webhooks from messaging platforms (WhatsApp Cloud API, QQ bots)."""

import hashlib
import hmac
import json
import logging

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.platform import conf

from .models import Channel, OAuthApp
from .providers import qq, whatsapp

log = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    if request.method == "GET":
        # Meta's subscription handshake.
        token = request.GET.get("hub.verify_token", "")
        known = any(
            hmac.compare_digest(token, ch.config.get("verify_token", ""))
            for ch in Channel.objects.filter(provider="whatsapp")
        )
        if request.GET.get("hub.mode") == "subscribe" and known:
            return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
        return HttpResponse("Forbidden", status=403)
    secret = conf.get("META_APP_SECRET") or ""
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(signature, expected):
        return HttpResponse("Bad signature", status=401)
    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponse("Bad JSON", status=400)
    handled = whatsapp.handle_webhook(payload)
    return JsonResponse({"handled": handled})


def _qq_secret(app_id):
    app = OAuthApp.objects.filter(provider="qq", client_id=app_id).first()
    if app:
        return app.secrets.get("client_secret")
    ch = Channel.objects.filter(provider="qq").exclude(credentials__isnull=True)
    for c in ch:
        if c.credentials.get("app_id") == app_id:
            return c.credentials.get("app_secret")
    return None


@csrf_exempt
@require_http_methods(["POST"])
def qq_webhook(request, app_id):
    secret = _qq_secret(app_id)
    if not secret:
        return HttpResponse("Unknown bot", status=404)
    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponse("Bad JSON", status=400)
    if event.get("op") == 13:  # callback URL validation
        d = event.get("d") or {}
        return JsonResponse(
            {
                "plain_token": d.get("plain_token", ""),
                "signature": qq.sign_validation(secret, d.get("event_ts", ""), d.get("plain_token", "")),
            }
        )
    ts = request.headers.get("X-Signature-Timestamp", "")
    sig = request.headers.get("X-Signature-Ed25519", "")
    if not qq.verify_signature(secret, ts, request.body, sig):
        return HttpResponse("Bad signature", status=401)
    if event.get("op") == 0:
        qq.record_event(app_id, event)
    return JsonResponse({"op": 12})

"""Sending one delivery: email, a Telegram status bot, or a signed webhook."""

import hashlib
import hmac
import html
import json
import time

import httpx
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection

from apps.platform import conf

from .events import render
from .models import Kind

TELEGRAM = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"


class DeliveryError(Exception):
    def __init__(self, message, *, retryable=False, status=None, retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


def send(destination, delivery):
    """Sends ``delivery`` to ``destination``; returns the HTTP status (if any) or raises DeliveryError."""
    return {Kind.EMAIL: send_email, Kind.TELEGRAM: send_telegram, Kind.WEBHOOK: send_webhook}[destination.kind](
        destination, delivery.payload, str(delivery.uid)
    )


# --- email ---------------------------------------------------------------------------------
def email_configured():
    return bool(conf.get("EMAIL_HOST")) or settings.EMAIL_BACKEND != SMTP_BACKEND


def _connection():
    if conf.get("EMAIL_HOST"):
        return get_connection(
            SMTP_BACKEND,
            host=conf.get("EMAIL_HOST"),
            port=int(conf.get("EMAIL_PORT") or 587),
            username=conf.get("EMAIL_HOST_USER") or "",
            password=conf.get("EMAIL_HOST_PASSWORD") or "",
            use_tls=bool(conf.get("EMAIL_USE_TLS")),
            timeout=20,
        )
    return get_connection()


def recipients(destination):
    to = [a for a in (destination.config or {}).get("to") or [] if a]
    if to:
        return to
    # Nobody listed: the workspace's admins and owners, including the organization's.
    from apps.core.models import User
    from apps.tenants.models import OrgMembership, OrgRole, Role

    ws = destination.workspace
    users = User.objects.filter(is_active=True).filter(
        pk__in=list(ws.memberships.filter(role__in=(Role.OWNER, Role.ADMIN)).values_list("user_id", flat=True))
        + list(
            OrgMembership.objects.filter(
                organization_id=ws.organization_id, role__in=(OrgRole.OWNER, OrgRole.ADMIN)
            ).values_list("user_id", flat=True)
        )
    )
    return sorted({u.email for u in users})


def send_email(destination, payload, uid):
    if not email_configured():
        raise DeliveryError("This server has no email settings (EMAIL_HOST).")
    to = recipients(destination)
    if not to:
        raise DeliveryError("Nobody to send it to.")
    subject, lines, link = render(payload)
    body = "\n\n".join([*lines, link]) + f"\n\n— Markette · {payload['workspace']['name']}\n"
    html_lines = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    html_body = (
        f"<p><strong>{html.escape(subject)}</strong></p>{html_lines}"
        + (f'<p><a href="{html.escape(link)}">Open in Markette</a></p>' if link else "")
        + f'<p style="color:#888;font-size:12px">Markette · {html.escape(payload["workspace"]["name"])} · '
        "notification settings are under Settings → Notifications.</p>"
    )
    msg = EmailMultiAlternatives(
        subject=f"[Markette] {subject}"[:250],
        body=body,
        from_email=conf.get("DEFAULT_FROM_EMAIL") or settings.DEFAULT_FROM_EMAIL,
        to=to,
        headers={"X-Markette-Delivery": uid},
        connection=_connection(),
    )
    msg.attach_alternative(html_body, "text/html")
    try:
        msg.send()
    except Exception as exc:  # noqa: BLE001 - SMTP errors come in many shapes
        raise DeliveryError(f"Email could not be sent: {exc}", retryable=True) from exc
    return None


# --- Telegram ------------------------------------------------------------------------------
def telegram_call(token, method, **params):
    try:
        resp = httpx.post(f"{TELEGRAM}/bot{token}/{method}", json=params, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise DeliveryError(f"Telegram could not be reached: {exc}", retryable=True) from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code == 200 and body.get("ok"):
        return body.get("result")
    desc = body.get("description") or f"HTTP {resp.status_code}"
    retry_after = (body.get("parameters") or {}).get("retry_after")
    retryable = resp.status_code == 429 or resp.status_code >= 500
    raise DeliveryError(f"Telegram: {desc}", retryable=retryable, status=resp.status_code, retry_after=retry_after)


def telegram_text(payload):
    subject, lines, link = render(payload)
    parts = [f"<b>{html.escape(subject)}</b>", *(html.escape(line) for line in lines)]
    if link:
        parts.append(f'<a href="{html.escape(link)}">Open in Markette</a>')
    return "\n".join(parts)[:4000]


def send_telegram(destination, payload, uid):
    token = (destination.secrets or {}).get("bot_token")
    chat = (destination.config or {}).get("chat_id")
    if not token or not chat:
        raise DeliveryError("The bot token or the chat is missing.")
    telegram_call(
        token,
        "sendMessage",
        chat_id=chat,
        text=telegram_text(payload),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return 200


def telegram_chats(token):
    """The bot's name and the chats that wrote to it or added it recently (for picking one)."""
    me = telegram_call(token, "getMe")
    updates = telegram_call(
        token, "getUpdates", limit=100, allowed_updates=["message", "channel_post", "my_chat_member"]
    )
    chats = {}
    for u in updates or []:
        for key in ("message", "channel_post", "my_chat_member"):
            chat = (u.get(key) or {}).get("chat")
            if chat:
                title = (
                    chat.get("title")
                    or " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
                    or chat.get("username", "")
                )
                chats[chat["id"]] = {"id": chat["id"], "title": title, "type": chat.get("type", "")}
    return {"bot": me.get("username", ""), "chats": list(chats.values())}


# --- webhooks ------------------------------------------------------------------------------
def signature(secret, timestamp, body: bytes):
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def send_webhook(destination, payload, uid):
    url = (destination.config or {}).get("url")
    secret = (destination.secrets or {}).get("signing_secret", "")
    body = json.dumps({"id": uid, **payload}, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Markette-Webhooks/1",
        "X-Markette-Event": payload["event"],
        "X-Markette-Delivery": uid,
        "X-Markette-Timestamp": ts,
        "X-Markette-Signature": signature(secret, ts, body),
    }
    try:
        resp = httpx.post(url, content=body, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise DeliveryError(f"Could not reach {url}: {exc}", retryable=True) from exc
    if 200 <= resp.status_code < 300:
        return resp.status_code
    retryable = resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500
    retry_after = resp.headers.get("retry-after")
    raise DeliveryError(
        f"{url} answered {resp.status_code}: {resp.text[:300]}",
        retryable=retryable,
        status=resp.status_code,
        retry_after=int(retry_after) if (retry_after or "").isdigit() else None,
    )

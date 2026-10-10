"""Generic outgoing webhook: every publication is POSTed as JSON to your URL.

Use it to connect n8n, Make, Zapier, Node-RED or your own code to anything Markette
does not support natively. Requests are signed: ``X-Markette-Signature: sha256=<hmac>``
over ``<X-Markette-Timestamp>.<raw body>`` with the channel's signing secret.

If your endpoint answers with JSON containing ``id`` and/or ``url``, Markette stores
them as the publication's id and link. 5xx and timeouts are retried with backoff.
"""

import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlparse

from django.utils import timezone

from .base import BaseProvider, Candidate, Capabilities, CredentialField, ProviderError, PublishResult


def sign(secret, timestamp, body: bytes):
    return "sha256=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


class WebhookProvider(BaseProvider):
    key = "webhook"
    label = "Webhook"
    capabilities = Capabilities(
        max_chars=None,
        max_media=20,
        auth="credentials",
        has_metrics=False,
        metric_names=(),
    )
    credential_fields = [
        CredentialField("url", "Endpoint URL", type="url", help="e.g. an n8n Webhook node's production URL."),
        CredentialField("name", "Name", required=False, help="How this destination appears in Markette."),
        CredentialField(
            "headers",
            "Extra headers",
            type="textarea",
            required=False,
            help="One per line, e.g. Authorization: Bearer abc",
        ),
    ]

    @staticmethod
    def _parse_headers(text):
        headers = {}
        for line in (text or "").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip():
                    headers[k.strip()] = v.strip()
        return headers

    def _post(self, url, secret, headers, payload):
        body = json.dumps(payload, default=str, ensure_ascii=False).encode()
        ts = str(int(time.time()))
        return self.http(
            "POST",
            url,
            content=body,
            expect=tuple(range(200, 300)),
            headers={
                **headers,
                "Content-Type": "application/json",
                "User-Agent": "Markette-Webhook/1.0",
                "X-Markette-Event": payload["event"],
                "X-Markette-Timestamp": ts,
                "X-Markette-Signature": sign(secret, ts, body),
            },
        )

    def connect_with_credentials(self, data):
        url = (data.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ProviderError("Enter a full http(s) URL.")
        secret = secrets.token_hex(24)
        headers = self._parse_headers(data.get("headers"))
        try:
            self._post(
                url,
                secret,
                headers,
                {
                    "event": "ping",
                    "sent_at": timezone.now().isoformat(),
                    "message": "Markette is checking this webhook.",
                },
            )
        except ProviderError as exc:
            raise ProviderError(f"The endpoint did not accept a test request: {exc}") from exc
        return [
            Candidate(
                external_id=f"{parsed.netloc}{parsed.path}"[:255],
                name=(data.get("name") or parsed.netloc)[:255],
                handle=parsed.netloc,
                profile_url="",
                credentials={"url": url, "secret": secret, "headers": headers},
            )
        ]

    def publish(self, content):
        target_id = content.idempotency_key.rsplit("-", 1)[-1]
        payload = {
            "event": "publication.created",
            "idempotency_key": content.idempotency_key,
            "sent_at": timezone.now().isoformat(),
            "target_id": int(target_id) if target_id.isdigit() else target_id,
            "channel": {"id": self.channel.pk, "name": self.channel.name},
            "post": {
                "title": content.title,
                "text": content.text,
                "link": content.link,
                "tags": content.tags,
                "options": content.options,
                "media": [
                    {"url": m.url, "mime": m.mime, "alt": m.alt, "width": m.width, "height": m.height, "size": m.size}
                    for m in content.media
                ],
            },
        }
        resp = self._post(self.creds["url"], self.creds["secret"], self.creds.get("headers") or {}, payload)
        try:
            answer = resp.json() if resp.content else {}
        except ValueError:
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        return PublishResult(
            external_id=str(answer.get("id") or content.idempotency_key)[:500], url=str(answer.get("url") or "")[:1000]
        )

    def delete(self, external_id):
        self._post(
            self.creds["url"],
            self.creds["secret"],
            self.creds.get("headers") or {},
            {"event": "publication.deleted", "external_id": external_id, "sent_at": timezone.now().isoformat()},
        )

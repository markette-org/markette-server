"""Email newsletters: Brevo, Listmonk (self-hosted) and Ghost (newsletter + blog).

A channel is one mailing list (Brevo, Listmonk) or one Ghost site. Each publication
becomes a campaign: subject = title, body = the post rendered from Markdown into a
simple, mobile-friendly HTML email with an unsubscribe link.
"""

import html as htmllib
import time

import markdown as md

from .base import (
    BaseProvider,
    Candidate,
    Capabilities,
    CredentialField,
    Metrics,
    ProviderError,
    PublishResult,
    repeatable,
)
from .blogs import normalize_site

NEWSLETTER_CAPS = dict(
    kind="newsletter",
    max_chars=None,
    max_media=20,
    supports_video=False,
    supports_title=True,
    auth="credentials",
    metric_names=("impressions", "reach", "clicks"),
)


def render_email(content, unsubscribe_html):
    """A minimal responsive email: hero image, title, body, call to action, footer."""
    body = md.markdown(content.text or "", extensions=["extra", "sane_lists", "smarty"])
    hero = ""
    if content.media:
        m = content.media[0]
        if m.url not in (content.text or ""):
            hero = (
                f'<img src="{htmllib.escape(m.url)}" alt="{htmllib.escape(m.alt)}" width="600" '
                'style="width:100%;max-width:600px;height:auto;border-radius:8px;display:block;margin:0 0 20px">'
            )
    cta = ""
    if content.link:
        cta = (
            f'<p style="margin:28px 0"><a href="{htmllib.escape(content.link)}" style="background:#5b4bf5;'
            'color:#fff;padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:600">Read more</a></p>'
        )
    preheader = htmllib.escape((content.options.get("preheader") or "")[:200])
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{htmllib.escape(content.title)}</title></head>
<body style="margin:0;background:#f4f4f7;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d1d29">
<span style="display:none;max-height:0;overflow:hidden">{preheader}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" style="max-width:600px;background:#fff;border-radius:12px" cellpadding="0" cellspacing="0">
<tr><td style="padding:28px 28px 8px">{hero}<h1 style="font-size:24px;line-height:1.25;margin:0 0 16px">{htmllib.escape(content.title)}</h1>
<div style="font-size:16px;line-height:1.6">{body}</div>{cta}</td></tr></table>
<p style="font-size:12px;color:#7a7695;margin:16px 0">{unsubscribe_html}</p>
</td></tr></table></body></html>"""


# --- Brevo -----------------------------------------------------------------------------------

BREVO = "https://api.brevo.com/v3"


class BrevoProvider(BaseProvider):
    key = "brevo"
    label = "Brevo"
    capabilities = Capabilities(**NEWSLETTER_CAPS)
    credential_fields = [
        CredentialField("api_key", "API key", type="password", help="Brevo → SMTP & API → API keys."),
        CredentialField("sender_email", "Sender email", help="A sender verified in Brevo → Senders & IPs."),
        CredentialField("sender_name", "Sender name", required=False),
    ]

    def _h(self, key=None):
        return {"api-key": key or self.creds["api_key"], "accept": "application/json"}

    def connect_with_credentials(self, data):
        key = data["api_key"].strip()
        account = self.http("GET", f"{BREVO}/account", headers=self._h(key)).json()
        senders = self.http("GET", f"{BREVO}/senders", headers=self._h(key)).json().get("senders", [])
        email = (data.get("sender_email") or "").strip().lower()
        sender = next((s for s in senders if s.get("email", "").lower() == email and s.get("active", True)), None)
        if sender is None:
            raise ProviderError(f"{email} is not an active sender in this Brevo account (Senders & IPs).")
        lists, offset = [], 0
        while True:
            page = self.http(
                "GET", f"{BREVO}/contacts/lists", headers=self._h(key), params={"limit": 50, "offset": offset}
            ).json()
            lists += page.get("lists", [])
            offset += 50
            if offset >= page.get("count", 0):
                break
        if not lists:
            raise ProviderError("This Brevo account has no contact lists yet.")
        company = (account.get("companyName") or "Brevo").strip()
        return [
            Candidate(
                external_id=str(lst["id"]),
                name=lst["name"],
                handle=f"{company} · Brevo list",
                credentials={"api_key": key},
                config={
                    "sender_email": sender["email"],
                    "sender_name": data.get("sender_name") or sender.get("name", ""),
                },
                followers_count=lst.get("uniqueSubscribers") or lst.get("totalSubscribers"),
            )
            for lst in lists
        ]

    def publish(self, content):
        cfg = self.channel.config
        html = render_email(content, '<a href="{{ unsubscribe }}" style="color:#7a7695">Unsubscribe</a>')
        sender = {"email": cfg["sender_email"]}
        if cfg.get("sender_name"):
            sender["name"] = cfg["sender_name"]
        payload = {
            "name": f"{content.title[:200]} ({content.idempotency_key})",
            "subject": content.title,
            "sender": sender,
            "htmlContent": html,
            "recipients": {"listIds": [int(self.channel.external_id)]},
            "inlineImageActivation": False,
        }
        if content.options.get("preheader"):
            payload["previewText"] = content.options["preheader"][:200]
        camp = self.http("POST", f"{BREVO}/emailCampaigns", headers=self._h(), json=payload).json()
        self.http("POST", f"{BREVO}/emailCampaigns/{camp['id']}/sendNow", headers=self._h())
        return PublishResult(external_id=str(camp["id"]), url="")

    def fetch_metrics(self, external_ids):
        out = {}
        for cid in external_ids:
            data = self.http(
                "GET",
                f"{BREVO}/emailCampaigns/{cid}",
                headers=self._h(),
                params={"statistics": "globalStats", "excludeHtmlContent": "true"},
            ).json()
            g = ((data.get("statistics") or {}).get("globalStats")) or {}
            out[cid] = Metrics(
                impressions=g.get("delivered"), reach=g.get("uniqueViews"), clicks=g.get("uniqueClicks"), raw=g
            )
        return out

    def fetch_audience(self):
        lst = self.http("GET", f"{BREVO}/contacts/lists/{self.channel.external_id}", headers=self._h()).json()
        return lst.get("uniqueSubscribers") or lst.get("totalSubscribers")


# --- Listmonk ------------------------------------------------------------------------------------


class ListmonkProvider(BaseProvider):
    key = "listmonk"
    label = "Listmonk"
    capabilities = Capabilities(**{**NEWSLETTER_CAPS, "instance_url": True})
    credential_fields = [
        CredentialField("base_url", "Listmonk URL", type="url", help="e.g. https://lists.example.org"),
        CredentialField("api_user", "API user", help="Admin → Users → New API user."),
        CredentialField("api_token", "API token", type="password"),
        CredentialField("from_email", "From", required=False, help="e.g. Acme <news@acme.org>; default from settings."),
        CredentialField(
            "template_id", "Template ID", required=False, help="Listmonk campaign template; default if empty."
        ),
    ]

    def _h(self, creds=None):
        c = creds or self.creds
        return {"Authorization": f"token {c['api_user']}:{c['api_token']}"}

    def _api(self, method, path, base=None, creds=None, **kwargs):
        base = base or self.channel.base_url
        resp = self.http(method, f"{base}/api/{path.lstrip('/')}", headers=self._h(creds), **kwargs)
        return resp.json().get("data") if resp.content else None

    def connect_with_credentials(self, data):
        base = normalize_site(data.get("base_url"))
        creds = {"api_user": data["api_user"].strip(), "api_token": data["api_token"].strip()}
        lists = self._api("GET", "lists", base, creds, params={"per_page": "all"}).get("results", [])
        if not lists:
            raise ProviderError("This Listmonk instance has no lists yet.")
        config = {
            "from_email": (data.get("from_email") or "").strip(),
            "template_id": int(data["template_id"]) if str(data.get("template_id") or "").isdigit() else None,
        }
        return [
            Candidate(
                external_id=str(lst["id"]),
                name=lst["name"],
                handle=f"Listmonk · {lst.get('type', 'list')}",
                base_url=base,
                profile_url=base,
                credentials=creds,
                config=config,
                followers_count=lst.get("subscriber_count"),
            )
            for lst in lists
        ]

    def publish(self, content):
        cfg = self.channel.config
        body = render_email(content, '<a href="{{ UnsubscribeURL }}" style="color:#7a7695">Unsubscribe</a>')
        payload = {
            "name": content.title[:200],
            "subject": content.title,
            "lists": [int(self.channel.external_id)],
            "type": "regular",
            "content_type": "html",
            "body": body,
            "messenger": "email",
            "tags": ["markette"],
        }
        if cfg.get("from_email"):
            payload["from_email"] = cfg["from_email"]
        if cfg.get("template_id"):
            payload["template_id"] = cfg["template_id"]
        camp = self._api("POST", "campaigns", json=payload)
        self._api("PUT", f"campaigns/{camp['id']}/status", json={"status": "running"})
        return PublishResult(external_id=str(camp["id"]), url=f"{self.channel.base_url}/admin/campaigns/{camp['id']}")

    def fetch_metrics(self, external_ids):
        out = {}
        for cid in external_ids:
            c = self._api("GET", f"campaigns/{cid}")
            out[cid] = Metrics(
                impressions=c.get("sent"),
                reach=c.get("views"),
                clicks=c.get("clicks"),
                raw={k: c.get(k) for k in ("sent", "to_send", "views", "clicks", "bounces", "status")},
            )
        return out

    def fetch_audience(self):
        lst = self._api("GET", f"lists/{self.channel.external_id}")
        return (lst or {}).get("subscriber_count")


# --- Ghost --------------------------------------------------------------------------------------------


class GhostProvider(BaseProvider):
    key = "ghost"
    label = "Ghost"
    capabilities = Capabilities(**{**NEWSLETTER_CAPS, "kind": "blog", "metric_names": ("impressions", "reach")})
    credential_fields = [
        CredentialField(
            "base_url", "Ghost admin URL", type="url", help="e.g. https://blog.example.com or https://x.ghost.io"
        ),
        CredentialField(
            "admin_key",
            "Admin API key",
            type="password",
            help="Ghost Admin → Settings → Integrations → Add custom integration → Admin API key.",
        ),
        CredentialField(
            "newsletter",
            "Send as email to",
            required=False,
            help="Newsletter slug to email each post (e.g. default-newsletter); empty = web only.",
        ),
    ]

    @staticmethod
    def _jwt(admin_key):
        import jwt

        try:
            kid, secret = admin_key.split(":")
            key = bytes.fromhex(secret)
        except ValueError as exc:
            raise ProviderError("The Ghost Admin API key must look like <id>:<hex secret>.") from exc
        now = int(time.time())
        return jwt.encode(
            {"iat": now, "exp": now + 300, "aud": "/admin/"}, key, algorithm="HS256", headers={"kid": kid, "typ": "JWT"}
        )

    def _api(self, method, path, base=None, key=None, **kwargs):
        base = base or self.channel.base_url
        headers = {"Authorization": f"Ghost {self._jwt(key or self.creds['admin_key'])}", "Accept-Version": "v5.0"}
        resp = self.http(method, f"{base}/ghost/api/admin/{path.lstrip('/')}", headers=headers, **kwargs)
        return resp.json() if resp.content else {}

    def connect_with_credentials(self, data):
        base = normalize_site(data.get("base_url"))
        key = data["admin_key"].strip()
        site = self._api("GET", "site/", base, key).get("site", {})
        newsletter = (data.get("newsletter") or "").strip()
        if newsletter:
            slugs = [n["slug"] for n in self._api("GET", "newsletters/", base, key).get("newsletters", [])]
            if newsletter not in slugs:
                raise ProviderError(f"Newsletter '{newsletter}' not found. Available: {', '.join(slugs) or 'none'}.")
        try:
            members = (
                self._api("GET", "members/", base, key, params={"limit": 1})
                .get("meta", {})
                .get("pagination", {})
                .get("total")
            )
        except ProviderError:
            members = None
        return [
            Candidate(
                external_id=site.get("url") or base,
                name=site.get("title") or base,
                handle=(site.get("url") or base).split("://")[-1],
                avatar_url=site.get("icon") or site.get("logo") or "",
                profile_url=site.get("url") or base,
                base_url=base,
                credentials={"admin_key": key},
                config={"newsletter": newsletter},
                followers_count=members,
            )
        ]

    @repeatable
    def _upload(self, item):
        res = self._api(
            "POST",
            "images/upload/",
            files={"file": (item.filename or "image.jpg", item.read(), item.mime)},
            data={"purpose": "image"},
        )
        return res["images"][0]["url"]

    def publish(self, content):
        from .blogs import inline_images, render_html

        opts, cfg = content.options, self.channel.config
        uploaded = {m.url: self._upload(m) for m in content.media}
        html = inline_images(render_html(content.text), content.media, uploaded)
        post = {
            "title": content.title,
            "html": html,
            "status": "draft" if opts.get("status") == "draft" else "published",
        }
        if content.media:
            post["feature_image"] = uploaded[content.media[0].url]
            if content.media[0].alt:
                post["feature_image_alt"] = content.media[0].alt[:125]
        if opts.get("excerpt"):
            post["custom_excerpt"] = opts["excerpt"][:300]
        if opts.get("slug"):
            post["slug"] = opts["slug"]
        tags = opts.get("tags") or content.tags
        if tags:
            post["tags"] = [{"name": t} for t in tags]
        params = {"source": "html"}
        newsletter = opts.get("newsletter", cfg.get("newsletter"))
        if newsletter and post["status"] == "published":
            params.update(newsletter=newsletter, email_segment=opts.get("email_segment") or "all")
        created = self._api("POST", "posts/", params=params, json={"posts": [post]})["posts"][0]
        return PublishResult(
            external_id=created["id"], url=created.get("url", ""), extra={"emailed": bool(params.get("newsletter"))}
        )

    def delete(self, external_id):
        self._api("DELETE", f"posts/{external_id}/")

    def fetch_metrics(self, external_ids):
        out = {}
        for pid in external_ids:
            try:
                post = self._api("GET", f"posts/{pid}/", params={"include": "email"})["posts"][0]
            except ProviderError as exc:
                if exc.status == 404:
                    continue
                raise
            email = post.get("email") or {}
            out[pid] = Metrics(
                impressions=email.get("delivered_count"),
                reach=email.get("opened_count"),
                raw={k: email.get(k) for k in ("email_count", "delivered_count", "opened_count", "failed_count")},
            )
        return out

    def fetch_audience(self):
        return self._api("GET", "members/", params={"limit": 1}).get("meta", {}).get("pagination", {}).get("total")

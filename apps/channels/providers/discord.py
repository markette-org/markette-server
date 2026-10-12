"""Discord channels through channel webhooks (Server Settings → Integrations → Webhooks).

No bot or app review needed. Forum channels need a thread name (taken from the post
title). Messages are limited to 2000 characters; long texts are split.
"""

import json
import re

from .base import BaseProvider, Candidate, Capabilities, CredentialField, ProviderError, PublishResult

WEBHOOK_RE = re.compile(r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/(\d+)/([\w-]+)$")
LIMIT = 2000


def split_text(text, limit=LIMIT):
    parts, rest = [], text.strip()
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else rest.rfind(" ", 0, limit)
        cut = cut if cut > 0 else limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


class DiscordProvider(BaseProvider):
    key = "discord"
    label = "Discord"
    capabilities = Capabilities(
        max_chars=6000,  # split into up to three messages
        max_media=10,
        supports_edit=True,
        auth="credentials",
        has_metrics=False,
        metric_names=(),
    )
    credential_fields = [
        CredentialField(
            "webhook_url",
            "Webhook URL",
            type="password",
            help="Channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.",
        ),
        CredentialField("username", "Post as (name)", required=False, help="Overrides the webhook's name."),
    ]

    def connect_with_credentials(self, data):
        url = data["webhook_url"].strip().split("?")[0]
        if not WEBHOOK_RE.match(url):
            raise ProviderError("That does not look like a Discord webhook URL.")
        hook = self.http("GET", url).json()
        avatar = f"https://cdn.discordapp.com/avatars/{hook['id']}/{hook['avatar']}.png" if hook.get("avatar") else ""
        return [
            Candidate(
                external_id=str(hook["channel_id"]),
                name=hook.get("name") or "Discord channel",
                handle=f"#{hook['channel_id']}",
                avatar_url=avatar,
                profile_url=f"https://discord.com/channels/{hook.get('guild_id')}/{hook['channel_id']}",
                credentials={"webhook_url": url},
                config={"guild_id": str(hook.get("guild_id") or ""), "username": (data.get("username") or "")[:80]},
            )
        ]

    def _send(self, payload, files=None, thread_id=None):
        params = {"wait": "true"}
        if thread_id:
            params["thread_id"] = thread_id
        url = self.creds["webhook_url"]
        if files:
            resp = self.http("POST", url, params=params, data={"payload_json": json.dumps(payload)}, files=files)
        else:
            resp = self.http("POST", url, params=params, json=payload)
        return resp.json()

    def publish(self, content):
        cfg = self.channel.config
        base = {"allowed_mentions": {"parse": []}}  # never ping @everyone by accident
        if cfg.get("username"):
            base["username"] = cfg["username"]
        parts = split_text(content.text) or [""]
        first = {**base, "content": parts[0]}
        if content.options.get("forum_thread") or cfg.get("forum"):
            first["thread_name"] = (content.title or parts[0][:80] or "Update")[:100]
        files = None
        if content.media:
            files = {f"files[{i}]": (m.filename or f"file{i}", m.read(), m.mime) for i, m in enumerate(content.media)}
            first["attachments"] = [
                {"id": i, "filename": m.filename or f"file{i}", "description": m.alt[:1024]}
                for i, m in enumerate(content.media)
            ]
        msg = self._send(first, files)
        thread_id = msg.get("channel_id") if first.get("thread_name") else None
        for part in parts[1:]:
            self._send({**base, "content": part}, thread_id=thread_id)
        guild = cfg.get("guild_id")
        url = f"https://discord.com/channels/{guild}/{msg['channel_id']}/{msg['id']}" if guild else ""
        return PublishResult(external_id=f"{msg['channel_id']}:{msg['id']}", url=url)

    def update(self, external_id, content, extra):
        # Only the first message is edited: text that grew past 2,000 characters does not fit.
        channel_id, _, message_id = external_id.partition(":")
        parts = split_text(content.text) or [""]
        if len(parts) > 1:
            raise ProviderError("The new text is longer than one Discord message (2,000 characters).")
        params = {"thread_id": channel_id} if channel_id != self.channel.external_id else None
        self.http(
            "PATCH",
            f"{self.creds['webhook_url']}/messages/{message_id}",
            params=params,
            json={"content": parts[0], "allowed_mentions": {"parse": []}},
        )

    def delete(self, external_id):
        channel_id, _, message_id = external_id.partition(":")
        params = {"thread_id": channel_id} if channel_id != self.channel.external_id else None
        self.http("DELETE", f"{self.creds['webhook_url']}/messages/{message_id}", params=params)

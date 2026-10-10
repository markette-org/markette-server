"""QQ groups and QQ Guild (频道) text channels through the official QQ bot API.

Groups: since 2026-06-22 a bot may post proactively in a group once the group owner
enables "机器人主动在群聊内发言" (allow the bot to speak proactively). QQ does not
expose a list of a bot's groups, so Markette learns them from the webhook events
(GROUP_ADD_ROBOT, GROUP_AT_MESSAGE_CREATE…) sent to its callback URL. QQ refuses
URLs in group messages.

Guild channels: listed through the API (/users/@me/guilds, /guilds/{id}/channels);
proactive posts there have a daily quota per channel.
"""

import time

from django.conf import settings

from .base import BaseProvider, Candidate, Capabilities, CredentialField, ProviderError, PublishResult
from .text import URL_RE

TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
API = "https://api.bot.qq.com"
TEXT_LIMIT = 2000
_token_cache: dict[str, tuple[float, str]] = {}


def callback_url(app_id):
    return f"{settings.PUBLIC_API_URL}/api/channels/qq/webhook/{app_id}/"


class QQProvider(BaseProvider):
    key = "qq"
    label = "QQ"
    capabilities = Capabilities(
        max_chars=TEXT_LIMIT,
        max_media=1,
        supports_video=False,
        auth="credentials",
        has_metrics=False,
        metric_names=(),
    )
    credential_fields = [
        CredentialField("app_id", "Bot AppID", help="QQ Open Platform (q.qq.com) → your bot → Development settings."),
        CredentialField("app_secret", "Bot AppSecret", type="password"),
    ]

    # --- auth -------------------------------------------------------------------------
    def _access_token(self, app_id=None, secret=None):
        app_id = app_id or self.creds["app_id"]
        secret = secret or self.creds["app_secret"]
        cached = _token_cache.get(app_id)
        if cached and cached[0] > time.time() + 60:
            return cached[1]
        resp = self.http("POST", TOKEN_URL, json={"appId": app_id, "clientSecret": secret}, expect=(200, 400, 401))
        body = resp.json()
        if not body.get("access_token"):
            raise ProviderError(f"QQ rejected the bot credentials: {body.get('message') or body}", auth=True)
        _token_cache[app_id] = (time.time() + int(body.get("expires_in", 7200)), body["access_token"])
        return body["access_token"]

    def _api(self, method, path, app_id=None, secret=None, **kwargs):
        headers = {"Authorization": f"QQBot {self._access_token(app_id, secret)}"}
        resp = self.http(method, f"{API}{path}", headers=headers, expect=(200, 201, 202, 204), **kwargs)
        return resp.json() if resp.content else {}

    def error_from_response(self, resp):
        err = super().error_from_response(resp)
        try:
            body = resp.json()
        except ValueError:
            return err
        code = body.get("code") or body.get("err_code")
        msg = body.get("message", "")
        hints = {
            40034105: "The group owner must enable 'allow the bot to speak proactively' (机器人主动在群聊内发言).",
            40034101: "The bot is not a member of this group.",
            40054003: "The bot is not a member of this group.",
            40054010: "QQ does not allow links in group messages.",
            40034100: "QQ's proactive message limit was reached; try later.",
            40054002: "The bot is muted in this group.",
        }
        if code in hints:
            err.args = (f"QQ: {hints[code]} ({code} {msg})",)
        if code in (40034100,) or resp.status_code == 429:
            err.retryable, err.retry_after = True, err.retry_after or 600
        if code in (40034101, 40054003, 11241, 11242, 11243):
            err.auth = True
        return err

    # --- connection ---------------------------------------------------------------------
    def connect_with_credentials(self, data):
        from ..models import InboundChat, OAuthApp

        app_id, secret = data["app_id"].strip(), data["app_secret"].strip()
        me = self._api("GET", "/users/@me", app_id, secret)
        # Keep the secret so the webhook can answer QQ's signed validation before any channel exists.
        OAuthApp.objects.update_or_create(
            provider=self.key,
            base_url=f"https://q.qq.com/bot/{app_id}",
            defaults={"client_id": app_id, "secrets": {"client_secret": secret}},
        )
        bot_name = me.get("username") or app_id
        creds = {"app_id": app_id, "app_secret": secret}
        candidates = []
        try:
            guilds = self._api("GET", "/users/@me/guilds", app_id, secret) or []
        except ProviderError:
            guilds = []
        for guild in guilds:
            for ch in self._api("GET", f"/guilds/{guild['id']}/channels", app_id, secret) or []:
                if ch.get("type") != 0:  # text channels only
                    continue
                candidates.append(
                    Candidate(
                        external_id=f"channel:{ch['id']}",
                        name=f"{guild.get('name', '')} · #{ch.get('name', '')}",
                        handle=f"QQ Guild via {bot_name}",
                        avatar_url=guild.get("icon", ""),
                        credentials=creds,
                        config={"kind": "channel", "guild_id": guild["id"], "bot_name": bot_name},
                    )
                )
        for chat in InboundChat.objects.filter(provider=self.key, app_id=app_id):
            candidates.append(
                Candidate(
                    external_id=f"group:{chat.chat_id}",
                    name=chat.title or f"QQ group {chat.chat_id[:8]}…",
                    handle=f"QQ group via {bot_name}",
                    credentials=creds,
                    config={"kind": "group", "bot_name": bot_name},
                )
            )
        if not candidates:
            raise ProviderError(
                payload={"callback_url": callback_url(app_id)},
                message=(
                    f"{bot_name} has no guild channels and no known groups yet. Set the bot's callback URL to "
                    f"{callback_url(app_id)} in the QQ Open Platform, add the bot to a group (and @-mention it once), "
                    "then connect again."
                ),
            )
        return candidates

    # --- publishing -----------------------------------------------------------------------
    def validate(self, content):
        problems = super().validate(content)
        has_link = URL_RE.search(content.text) or content.link
        if self.channel is not None and self.channel.config.get("kind") == "group" and has_link:
            problems.append("QQ does not allow links in group messages; remove them.")
        return problems

    def publish(self, content):
        kind, _, target = self.channel.external_id.partition(":")
        text = content.text.strip()
        if kind == "group":
            body = {"msg_type": 0, "content": text, "msg_seq": 1}
            if content.media:
                f = self._api(
                    "POST",
                    f"/v2/groups/{target}/files",
                    json={"file_type": 1, "url": content.media[0].url, "srv_send_msg": False},
                )
                body = {"msg_type": 7, "content": text or " ", "media": {"file_info": f["file_info"]}, "msg_seq": 1}
            res = self._api("POST", f"/v2/groups/{target}/messages", json=body)
            return PublishResult(external_id=f"group:{target}:{res.get('id', '')}", url="")
        body = {"content": text}
        if content.media:
            body["image"] = content.media[0].url
        res = self._api("POST", f"/channels/{target}/messages", json=body)
        return PublishResult(external_id=f"channel:{target}:{res.get('id', '')}", url="")

    def delete(self, external_id):
        kind, target, message_id = external_id.split(":", 2)
        path = (
            f"/v2/groups/{target}/messages/{message_id}"
            if kind == "group"
            else f"/channels/{target}/messages/{message_id}"
        )
        self._api("DELETE", path)


# --- webhook ---------------------------------------------------------------------------------


def _signing_key(secret):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    seed = secret.encode()
    while len(seed) < 32:
        seed += seed
    return Ed25519PrivateKey.from_private_bytes(seed[:32])


def sign_validation(secret, event_ts, plain_token):
    return _signing_key(secret).sign(f"{event_ts}{plain_token}".encode()).hex()


def verify_signature(secret, timestamp, body: bytes, signature_hex):
    from cryptography.exceptions import InvalidSignature

    try:
        _signing_key(secret).public_key().verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
        return True
    except (InvalidSignature, ValueError):
        return False


GROUP_EVENTS = ("GROUP_ADD_ROBOT", "GROUP_AT_MESSAGE_CREATE", "GROUP_MSG_RECEIVE")


def record_event(app_id, event):
    """Remember groups the bot is in; forget them when it is removed."""
    from ..models import InboundChat

    kind, d = event.get("t"), event.get("d") or {}
    group = d.get("group_openid")
    if not group:
        return
    if kind in GROUP_EVENTS:
        InboundChat.objects.update_or_create(provider="qq", app_id=app_id, chat_id=group, defaults={})
    elif kind == "GROUP_DEL_ROBOT":
        InboundChat.objects.filter(provider="qq", app_id=app_id, chat_id=group).delete()

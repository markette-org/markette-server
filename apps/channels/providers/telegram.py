"""Telegram channels (broadcasts) and groups through the Bot API.

The user creates a bot with @BotFather, adds it to the channel as an administrator
allowed to post (or to the group as a member that can send messages), and gives
Markette the bot token. Several channels can share one bot.
"""

from .base import BaseProvider, Candidate, Capabilities, CredentialField, ProviderError, PublishResult

API = "https://api.telegram.org"
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
CHAT_TYPES = {"channel": "Channel", "group": "Group", "supergroup": "Group"}


def message_url(chat, message_id):
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message_id}"
    chat_id = str(chat.get("id", ""))
    if chat_id.startswith("-100"):
        # Private supergroups and channels: t.me/c/<id without -100>/<message>
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return ""


class TelegramProvider(BaseProvider):
    key = "telegram"
    label = "Telegram"
    capabilities = Capabilities(
        max_chars=TEXT_LIMIT,
        max_media=10,
        auth="credentials",
        has_metrics=False,
        metric_names=(),
    )
    credential_fields = [
        CredentialField(
            "bot_token",
            "Bot token",
            type="password",
            help="Create a bot with @BotFather and paste its token. Add the bot to your channel as an "
            "administrator with 'Post messages', or to your group as a member.",
        ),
        CredentialField(
            "chat",
            "Channel or group",
            required=False,
            help="@channelname, a t.me link, or a numeric chat id (-100…). Leave empty to choose among the "
            "chats the bot was recently added to.",
        ),
    ]

    # --- Bot API ------------------------------------------------------------------
    def _call(self, method, token=None, *, files=None, **params):
        token = token or self.creds["bot_token"]
        kwargs = {"data": params, "files": files} if files else {"json": params}
        resp = self.http("POST", f"{API}/bot{token}/{method}", expect=(200, 400, 401, 403, 404, 429), **kwargs)
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError(
                f"Telegram returned an unreadable response ({resp.status_code}).", retryable=True
            ) from exc
        if body.get("ok"):
            return body["result"]
        desc = body.get("description", "unknown error")
        code = body.get("error_code", resp.status_code)
        retry_after = (body.get("parameters") or {}).get("retry_after")
        if code == 429:
            raise ProviderError(f"Telegram rate limit: {desc}", retryable=True, retry_after=retry_after, status=429)
        if code == 401:
            raise ProviderError("Telegram rejected the bot token.", auth=True, status=401)
        if code == 403:
            # Bot removed from the chat, or lost its admin rights.
            raise ProviderError(
                f"Telegram: {desc}. Check the bot is still in the chat with posting rights.", auth=True, status=403
            )
        raise ProviderError(f"Telegram error {code}: {desc}", status=code)

    @staticmethod
    def _normalize_chat(value):
        value = (value or "").strip()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if value.startswith(prefix):
                value = "@" + value[len(prefix) :].split("/")[0]
        if value and not value.startswith("@") and not value.lstrip("-").isdigit():
            value = "@" + value
        return value

    # --- connection -----------------------------------------------------------------
    def connect_with_credentials(self, data):
        token = data["bot_token"].strip()
        bot = self._call("getMe", token)
        chat_ref = self._normalize_chat(data.get("chat"))
        if chat_ref:
            chats = [self._call("getChat", token, chat_id=chat_ref)]
        else:
            chats = self._recent_chats(token)
            if not chats:
                raise ProviderError(
                    f"@{bot['username']} has not been added to any channel or group yet (or Telegram no longer "
                    "remembers it). Add the bot, post a message there, then try again, or enter the chat."
                )
        candidates = []
        for chat in chats:
            problem = self._posting_problem(token, bot, chat)
            if problem and chat_ref:
                raise ProviderError(problem)
            if problem:
                continue
            candidates.append(self._candidate(token, bot, chat))
        if not candidates:
            raise ProviderError(f"@{bot['username']} cannot post in any of the chats it was added to.")
        return candidates

    def _recent_chats(self, token):
        updates = self._call("getUpdates", token, allowed_updates=["my_chat_member", "message", "channel_post"])
        seen = {}
        for u in updates:
            for key in ("my_chat_member", "message", "channel_post"):
                chat = (u.get(key) or {}).get("chat")
                if chat and chat.get("type") in CHAT_TYPES:
                    seen[chat["id"]] = chat
        return [self._call("getChat", token, chat_id=cid) for cid in seen]

    def _posting_problem(self, token, bot, chat):
        member = self._call("getChatMember", token, chat_id=chat["id"], user_id=bot["id"])
        status = member.get("status")
        name = chat.get("title") or chat.get("username")
        if chat["type"] == "channel":
            if status != "administrator" or not member.get("can_post_messages", False):
                return f"Make @{bot['username']} an administrator of {name} with the 'Post messages' right."
        elif status in ("left", "kicked") or member.get("can_send_messages") is False:
            return f"@{bot['username']} cannot send messages in {name}."
        return None

    def _candidate(self, token, bot, chat):
        try:
            members = self._call("getChatMemberCount", token, chat_id=chat["id"])
        except ProviderError:
            members = None
        username = chat.get("username")
        return Candidate(
            external_id=str(chat["id"]),
            name=chat.get("title") or username or str(chat["id"]),
            handle=f"@{username}" if username else f"{CHAT_TYPES[chat['type']]} · via @{bot['username']}",
            profile_url=f"https://t.me/{username}" if username else "",
            credentials={"bot_token": token},
            config={"chat_type": chat["type"], "username": username or "", "bot_username": bot["username"]},
            followers_count=members,
        )

    # --- publishing -----------------------------------------------------------------
    def publish(self, content):
        chat_id = self.channel.external_id
        opts = content.options
        common = {"chat_id": chat_id}
        if opts.get("silent"):
            common["disable_notification"] = True
        if opts.get("protect_content"):
            common["protect_content"] = True
        text = content.text.strip()
        first = None
        if not content.media:
            first = self._call(
                "sendMessage",
                **common,
                text=text,
                link_preview_options={"is_disabled": bool(opts.get("disable_link_preview"))},
            )
        else:
            caption = text if len(text) <= CAPTION_LIMIT else ""
            if len(content.media) == 1:
                first = self._send_single(content.media[0], caption, common)
            else:
                first = self._send_album(content.media[:10], caption, common)
            if text and not caption:
                # Too long for a caption: follow the media with the full text.
                self._call("sendMessage", **common, text=text)
        message_id = first["message_id"]
        if opts.get("pin"):
            try:
                self._call(
                    "pinChatMessage",
                    chat_id=chat_id,
                    message_id=message_id,
                    disable_notification=bool(opts.get("silent")),
                )
            except ProviderError:
                pass  # Published; pinning needs the "Pin messages" right.
        chat = first.get("chat") or {"id": chat_id, "username": self.channel.config.get("username")}
        return PublishResult(external_id=f"{chat_id}:{message_id}", url=message_url(chat, message_id))

    def _send_single(self, item, caption, common):
        if item.is_video:
            method, field = "sendVideo", "video"
        elif item.mime == "image/gif":
            method, field = "sendAnimation", "animation"
        else:
            method, field = "sendPhoto", "photo"
        params = {k: str(v).lower() if isinstance(v, bool) else v for k, v in common.items()}
        if caption:
            params["caption"] = caption
        files = {field: (item.filename or field, item.read(), item.mime)}
        return self._call(method, files=files, **params)

    def _send_album(self, items, caption, common):
        import json

        media, files = [], {}
        for i, item in enumerate(items):
            name = f"file{i}"
            entry = {"type": "video" if item.is_video else "photo", "media": f"attach://{name}"}
            if i == 0 and caption:
                entry["caption"] = caption
            media.append(entry)
            files[name] = (item.filename or name, item.read(), item.mime)
        params = {k: str(v).lower() if isinstance(v, bool) else v for k, v in common.items()}
        messages = self._call("sendMediaGroup", files=files, media=json.dumps(media), **params)
        return messages[0]

    def delete(self, external_id):
        chat_id, _, message_id = external_id.rpartition(":")
        self._call("deleteMessage", chat_id=chat_id, message_id=int(message_id))

    def fetch_audience(self):
        return self._call("getChatMemberCount", chat_id=self.channel.external_id)

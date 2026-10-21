"""Telegram as a person (client API / MTProto, through Telethon), next to the bot-based provider.

Signing in takes an API ID and hash (my.telegram.org → API development tools), the phone
number, the login code Telegram sends, and the two-step verification password if there is
one. The resulting session is stored encrypted on each channel of that account.

Channels are the chats the account can write to (its channels, groups, people), published to
as the account itself, plus one **Direct messages** channel per account: it is not published
to; it brings the account's private chats into the inbox, where they can be answered.
"""

from contextlib import contextmanager

from .base import (
    BaseProvider,
    Candidate,
    Capabilities,
    CredentialField,
    InboxEntry,
    Metrics,
    ProviderError,
    PublishResult,
)

DM_PREFIX = "dm:"


def _telethon():
    import asyncio

    import telethon.sync  # noqa: F401 - makes the client's methods callable without await (workers are sync)
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    try:
        asyncio.get_event_loop()
    except RuntimeError:  # threads other than the main one have no loop yet
        asyncio.set_event_loop(asyncio.new_event_loop())
    return TelegramClient, StringSession, errors


def _error(exc):
    _, _, errors = _telethon()
    if isinstance(exc, errors.FloodWaitError):
        return ProviderError(f"Telegram asks to wait {exc.seconds} s.", retryable=True, retry_after=exc.seconds)
    if isinstance(exc, (errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.UserDeactivatedError)):
        return ProviderError("The Telegram session ended; sign in again.", auth=True)
    if isinstance(exc, errors.RPCError):
        return ProviderError(f"Telegram: {exc.__class__.__name__}: {exc}")
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return ProviderError(f"Telegram could not be reached: {exc}", retryable=True)
    return ProviderError(f"Telegram: {exc}")


@contextmanager
def client_for(api_id, api_hash, session=""):
    TelegramClient, StringSession, errors = _telethon()
    client = TelegramClient(StringSession(session or None), int(api_id), api_hash)
    try:
        client.connect()
        yield client
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - Telethon and network errors become provider errors
        raise _error(exc) from exc
    finally:
        client.disconnect()


# --- signing in (several steps; the pending state is kept encrypted between them) -----------------------
def login_start(api_id, api_hash, phone):
    TelegramClient, StringSession, errors = _telethon()
    try:
        with client_for(api_id, api_hash) as client:
            sent = client.send_code_request(phone)
            return {"session": client.session.save(), "phone_code_hash": sent.phone_code_hash}
    except ProviderError as exc:
        if "ApiIdInvalid" in str(exc):
            raise ProviderError("Telegram does not recognize this API ID and hash.") from exc
        if "PhoneNumberInvalid" in str(exc):
            raise ProviderError("Telegram does not recognize this phone number (use the +country format).") from exc
        raise


def login_finish(pending, api_id, api_hash, phone, code="", password=""):
    """Returns (session, candidates). Raises ProviderError with payload {"needs": "password"} for 2FA."""
    TelegramClient, StringSession, errors = _telethon()
    with client_for(api_id, api_hash, pending["session"]) as client:
        try:
            if password and not code:
                client.sign_in(password=password)
            else:
                client.sign_in(phone=phone, code=code, phone_code_hash=pending["phone_code_hash"])
        except errors.SessionPasswordNeededError:
            if not password:
                raise ProviderError(
                    "This account has a two-step verification password.", payload={"needs": "password"}
                ) from None
            client.sign_in(password=password)
        except errors.PhoneCodeInvalidError as exc:
            raise ProviderError("That code is not right.") from exc
        except errors.PhoneCodeExpiredError as exc:
            raise ProviderError("The code expired; start again.") from exc
        except errors.PasswordHashInvalidError as exc:
            raise ProviderError("That password is not right.", payload={"needs": "password"}) from exc
        session = client.session.save()
        return session, candidates(client, api_id, api_hash, session)


def candidates(client, api_id, api_hash, session):
    me = client.get_me()
    creds = {"api_id": int(api_id), "api_hash": api_hash, "session": session, "user_id": me.id}
    who = f"@{me.username}" if me.username else " ".join(p for p in (me.first_name, me.last_name) if p)
    out = [
        Candidate(
            external_id=f"{DM_PREFIX}{me.id}",
            name=f"Direct messages · {who}",
            handle=who,
            profile_url=f"https://t.me/{me.username}" if me.username else "",
            credentials=creds,
            config={"dm": True},
        )
    ]
    for d in client.iter_dialogs(limit=200):
        ent = d.entity
        can_post = (
            d.is_user
            or (d.is_group and not getattr(ent, "default_banned_rights", None))
            or (d.is_channel and (getattr(ent, "creator", False) or getattr(ent, "admin_rights", None)))
        )
        if not can_post or getattr(ent, "bot", False) or d.id == me.id:
            continue
        username = getattr(ent, "username", None)
        out.append(
            Candidate(
                external_id=str(d.id),
                name=d.name or str(d.id),
                handle=f"@{username}" if username else ("channel" if d.is_channel and not d.is_group else "chat"),
                profile_url=f"https://t.me/{username}" if username else "",
                credentials=creds,
                config={"username": username or "", "broadcast": bool(d.is_channel and not d.is_group)},
                followers_count=getattr(ent, "participants_count", None),
            )
        )
    return out


class TelegramUserProvider(BaseProvider):
    key = "telegram_user"
    label = "Telegram (personal account)"
    capabilities = Capabilities(
        max_chars=4096,
        max_media=10,
        auth="credentials",
        supports_edit=True,
        inbox=True,
        inbox_interval=2,
        inbox_reply=True,
        metric_names=("impressions", "comments", "shares", "likes"),
    )
    credential_fields = [
        CredentialField("api_id", "API ID", help="my.telegram.org → API development tools."),
        CredentialField("api_hash", "API hash", type="password"),
        CredentialField("phone", "Phone number", help="With the country code, e.g. +39 333 1234567."),
    ]

    def connect_with_credentials(self, data):
        raise ProviderError("Sign in step by step: send the code, then enter it.", payload={"steps": True})

    @property
    def is_dm(self):
        return self.channel.external_id.startswith(DM_PREFIX)

    @contextmanager
    def client(self):
        c = self.creds
        with client_for(c["api_id"], c["api_hash"], c["session"]) as client:
            yield client

    def _chat(self):
        cid = self.channel.external_id
        return int(cid) if cid.lstrip("-").isdigit() else cid

    def _url(self, message_id):
        username = (self.channel.config or {}).get("username")
        if username:
            return f"https://t.me/{username}/{message_id}"
        cid = str(self.channel.external_id)
        return f"https://t.me/c/{cid.removeprefix('-100')}/{message_id}" if cid.startswith("-100") else ""

    # --- publishing -------------------------------------------------------------------
    def validate(self, content):
        if self.channel is not None and self.is_dm:
            return ["This channel collects direct messages; pick a chat or channel to publish to."]
        problems = super().validate(content)
        if content.media and len(content.text) > 1024 and len(content.media) > 0:
            problems.append("With media, Telegram captions are limited to 1,024 characters.")
        return problems

    def publish(self, content):
        with self.client() as client:
            chat = self._chat()
            if content.media:
                sent = client.send_file(chat, [m.path for m in content.media], caption=content.text or None)
                first = sent[0] if isinstance(sent, list) else sent
            else:
                first = client.send_message(
                    chat, content.text, link_preview=not content.options.get("disable_link_preview")
                )
        return PublishResult(external_id=f"{self.channel.external_id}:{first.id}", url=self._url(first.id))

    def update(self, external_id, content, extra):
        message_id = int(external_id.rsplit(":", 1)[-1])
        with self.client() as client:
            client.edit_message(self._chat(), message_id, content.text)

    def delete(self, external_id):
        message_id = int(external_id.rsplit(":", 1)[-1])
        with self.client() as client:
            client.delete_messages(self._chat(), [message_id])

    def fetch_metrics(self, external_ids):
        if self.is_dm:
            return {}
        ids = {int(e.rsplit(":", 1)[-1]): e for e in external_ids}
        out = {}
        with self.client() as client:
            for msg in client.get_messages(self._chat(), ids=list(ids)):
                if msg is None:
                    continue
                reactions = sum(r.count for r in (getattr(getattr(msg, "reactions", None), "results", None) or []))
                out[ids[msg.id]] = Metrics(
                    impressions=msg.views,
                    shares=msg.forwards,
                    comments=getattr(getattr(msg, "replies", None), "replies", None),
                    likes=reactions or None,
                )
        return out

    # --- inbox -----------------------------------------------------------------------
    def fetch_inbox(self, state, recent_posts):
        last = dict(state.get("last") or {})
        entries = []
        with self.client() as client:
            if self.is_dm:
                for d in client.iter_dialogs(limit=50):
                    if not d.is_user or getattr(d.entity, "bot", False) or d.entity.is_self:
                        continue
                    key = str(d.id)
                    seen = int(last.get(key, 0))
                    if d.message is None or d.message.id <= seen:
                        continue
                    for msg in client.iter_messages(d.entity, min_id=seen, limit=20):
                        if not msg.out:
                            entries.append(self._entry(msg, d.entity, kind="message"))
                    last[key] = d.message.id
            else:
                ours = {int(e.rsplit(":", 1)[-1]) for e in recent_posts if ":" in e}
                seen = int(last.get("chat", 0))
                newest = seen
                for msg in client.iter_messages(self._chat(), min_id=seen, limit=100):
                    newest = max(newest, msg.id)
                    reply_to = getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None
                    if not msg.out and (reply_to in ours or msg.mentioned):
                        entries.append(
                            self._entry(msg, msg.sender, kind="reply" if reply_to in ours else "mention", post=reply_to)
                        )
                last["chat"] = newest
        return entries, {**state, "last": last}

    def _entry(self, msg, who, kind, post=None):
        username = getattr(who, "username", None)
        name = " ".join(p for p in (getattr(who, "first_name", None), getattr(who, "last_name", None)) if p) or getattr(
            who, "title", ""
        )
        chat_id = msg.chat_id
        return InboxEntry(
            external_id=f"{chat_id}:{msg.id}",
            kind=kind,
            text=msg.message or ("[media]" if msg.media else ""),
            url=f"https://t.me/{username}"
            if username and kind == "message"
            else self._url(msg.id)
            if kind != "message"
            else "",
            author_name=name or (f"@{username}" if username else str(getattr(who, "id", ""))),
            author_handle=f"@{username}" if username else "",
            author_url=f"https://t.me/{username}" if username else "",
            created_at=msg.date,
            post_external_id=f"{self.channel.external_id}:{post}" if post else "",
            context={"chat_id": chat_id, "message_id": msg.id},
        )

    def reply_inbox(self, entry, text):
        ctx = entry.context
        with self.client() as client:
            sent = client.send_message(ctx["chat_id"], text, reply_to=ctx["message_id"])
        return PublishResult(external_id=f"{ctx['chat_id']}:{sent.id}", url="")

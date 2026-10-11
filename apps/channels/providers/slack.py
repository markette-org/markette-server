"""Slack channels through a bot token (chat:write).

Two ways to connect:

* **Add to Slack** (OAuth v2) when the server has a Slack app (``SLACK_CLIENT_ID`` and
  ``SLACK_CLIENT_SECRET``, also editable in Platform → Settings).
* Otherwise users paste the *Bot User OAuth Token* (``xoxb-…``) of a Slack app they
  installed in their workspace themselves.

Either way the token can post in every public channel (``chat:write.public``) and in the
private channels the app was added to, so one connection lists them all and each chosen
conversation becomes a Markette channel. Images are uploaded with the files API and shared
in the same message as the text.
"""

import re
import time
from urllib.parse import urlencode

from apps.platform import conf

from .base import BaseProvider, Candidate, Capabilities, CredentialField, ProviderError, PublishResult, redirect_uri

API = "https://slack.com/api"
AUTHORIZE = "https://slack.com/oauth/v2/authorize"
SCOPES = [
    "chat:write",
    "chat:write.public",
    "channels:read",
    "channels:join",
    "groups:read",
    "files:write",
    "files:read",
    "team:read",
]
# Slack truncates messages longer than this.
TEXT_LIMIT = 40000
AUTH_ERRORS = {
    "not_authed",
    "invalid_auth",
    "account_inactive",
    "token_revoked",
    "token_expired",
    "no_permission",
    "missing_scope",
    "org_login_required",
    "ekm_access_denied",
    "team_access_not_granted",
}
RETRYABLE_ERRORS = {"internal_error", "fatal_error", "service_unavailable", "request_timeout", "ratelimited"}
CHANNEL_ID_RE = re.compile(r"^[CG][A-Z0-9]{6,}$")
ARCHIVES_RE = re.compile(r"/archives/([CG][A-Z0-9]{6,})")


def escape(text):
    """Slack treats &, < and > as markup; everything else (URLs included) is sent as is."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackProvider(BaseProvider):
    key = "slack"
    label = "Slack"
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
            help="api.slack.com/apps → your app → OAuth & Permissions → Install to Workspace → Bot User OAuth "
            "Token (xoxb-…). Bot scopes: " + ", ".join(SCOPES) + ".",
        ),
        CredentialField(
            "channel",
            "Channel",
            required=False,
            help="#channel-name, a channel ID or a link to the channel. Leave empty to choose among all the "
            "channels the app can post in. For a private channel, first add the app to it.",
        ),
    ]

    # --- configuration --------------------------------------------------------------
    @classmethod
    def oauth_configured(cls):
        return bool(conf.get("SLACK_CLIENT_ID") and conf.get("SLACK_CLIENT_SECRET"))

    @classmethod
    def auth_mode(cls):
        return "oauth2" if cls.oauth_configured() else "credentials"

    @classmethod
    def describe(cls):
        data = super().describe()
        if data["auth"] == "oauth2":
            data["credential_fields"] = []
        return data

    # --- Web API ----------------------------------------------------------------------
    def _call(self, method, token=None, *, http="POST", form=False, **params):
        token = token or self.creds["bot_token"]
        headers = {"Authorization": f"Bearer {token}"}
        if http == "GET":
            kwargs = {"params": params}
        elif form:
            kwargs = {"data": params}
        else:
            kwargs = {"json": params}
        resp = self.http(http, f"{API}/{method}", expect=(200, 429), headers=headers, **kwargs)
        if resp.status_code == 429:
            raise ProviderError(
                "Slack rate limit reached.",
                retryable=True,
                retry_after=int(resp.headers.get("retry-after") or 30),
                status=429,
            )
        return self._result(resp)

    def _result(self, resp):
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError("Slack returned an unreadable response.", retryable=True) from exc
        if body.get("ok"):
            return body
        error = body.get("error", "unknown_error")
        if error in AUTH_ERRORS:
            needed = f" (needs {body['needed']})" if body.get("needed") else ""
            raise ProviderError(f"Slack: {error}{needed}. Reconnect the Slack app.", auth=True, payload=body)
        if error in RETRYABLE_ERRORS:
            raise ProviderError(f"Slack: {error}", retryable=True, payload=body)
        raise ProviderError(f"Slack: {error}", payload=body)

    # --- connection -------------------------------------------------------------------
    def authorization_url(self, request):
        params = {
            "client_id": conf.get("SLACK_CLIENT_ID"),
            "scope": ",".join(SCOPES),
            "redirect_uri": redirect_uri(self.key),
            "state": request.state,
        }
        return f"{AUTHORIZE}?{urlencode(params)}"

    def complete_oauth(self, request, params):
        resp = self.http(
            "POST",
            f"{API}/oauth.v2.access",
            data={
                "code": params["code"],
                "redirect_uri": redirect_uri(self.key),
                "client_id": conf.get("SLACK_CLIENT_ID"),
                "client_secret": conf.get("SLACK_CLIENT_SECRET"),
            },
        )
        token = self._result(resp)["access_token"]
        return self._candidates(token)

    def connect_with_credentials(self, data):
        token = data["bot_token"].strip()
        if not token.startswith("xoxb-"):
            raise ProviderError("Paste the Bot User OAuth Token: it starts with xoxb-.")
        return self._candidates(token, (data.get("channel") or "").strip())

    def _candidates(self, token, channel_ref=""):
        auth = self._call("auth.test", token)
        team = {"id": auth.get("team_id", ""), "name": auth.get("team", ""), "url": auth.get("url", "")}
        try:
            info = self._call("team.info", token, http="GET")["team"]
            team["icon"] = (info.get("icon") or {}).get("image_132", "")
        except ProviderError:
            team["icon"] = ""
        if channel_ref:
            conv = self._find_channel(token, channel_ref)
            if conv.get("is_private") and not conv.get("is_member"):
                raise ProviderError(f"Add the app to #{conv['name']} first (channel → Integrations → Add apps).")
            return [self._candidate(token, team, conv)]
        candidates = [
            self._candidate(token, team, c)
            for c in self._conversations(token)
            if not c.get("is_archived") and (c.get("is_member") or not c.get("is_private"))
        ]
        if not candidates:
            raise ProviderError("The app cannot post in any channel yet. Add it to a channel and try again.")
        return candidates

    def _conversations(self, token):
        out, cursor = [], ""
        for _ in range(20):  # up to 20 000 channels
            params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            body = self._call("conversations.list", token, http="GET", **params)
            out += body.get("channels", [])
            cursor = (body.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
        return sorted(out, key=lambda c: c.get("name", ""))

    def _find_channel(self, token, ref):
        match = ARCHIVES_RE.search(ref)
        channel_id = match.group(1) if match else ref
        if CHANNEL_ID_RE.match(channel_id):
            try:
                return self._call("conversations.info", token, http="GET", channel=channel_id)["channel"]
            except ProviderError as exc:
                raise ProviderError(f"Slack cannot find channel {channel_id} ({exc}).") from exc
        name = ref.lstrip("#").lower()
        for conv in self._conversations(token):
            if conv.get("name") == name:
                return conv
        raise ProviderError(f"No channel named #{name} that the app can see. For a private channel, add the app first.")

    def _candidate(self, token, team, conv):
        base = (team.get("url") or "").rstrip("/")
        return Candidate(
            external_id=conv["id"],
            name=f"#{conv['name']}",
            handle=team["name"] + (" · private" if conv.get("is_private") else ""),
            avatar_url=team.get("icon", ""),
            profile_url=f"{base}/archives/{conv['id']}" if base else "",
            base_url=base,
            credentials={"bot_token": token},
            config={"team_id": team["id"], "team": team["name"], "private": bool(conv.get("is_private"))},
            followers_count=conv.get("num_members"),
        )

    # --- publishing -------------------------------------------------------------------
    def _text(self, content):
        text = content.text.strip()
        if content.link and content.link not in text:
            text = f"{text}\n{content.link}".strip()
        return escape(text)

    def publish(self, content):
        channel_id = self.channel.external_id
        text = self._text(content)
        if not content.media:
            msg = self._in_channel(
                lambda: self._call(
                    "chat.postMessage",
                    channel=channel_id,
                    text=text,
                    unfurl_links=not content.options.get("disable_link_preview"),
                    unfurl_media=True,
                )
            )
            return PublishResult(external_id=f"{channel_id}:{msg['ts']}", url=self._permalink(channel_id, msg["ts"]))
        file_ids = [self._upload(item) for item in content.media]
        self._in_channel(
            lambda: self._call(
                "files.completeUploadExternal",
                files=[{"id": fid} for fid in file_ids],
                channel_id=channel_id,
                initial_comment=text,
            )
        )
        ts = self._shared_ts(file_ids[0], channel_id)
        if ts:
            return PublishResult(external_id=f"{channel_id}:{ts}", url=self._permalink(channel_id, ts))
        # Slack shares files asynchronously; without the message timestamp keep the files.
        return PublishResult(external_id=f"{channel_id}:files:{','.join(file_ids)}", url="")

    def _in_channel(self, send):
        """Runs ``send``; if the app is not in this public channel yet, joins it and retries once."""
        try:
            return send()
        except ProviderError as exc:
            if not (isinstance(exc.payload, dict) and exc.payload.get("error") == "not_in_channel"):
                raise
        if self.channel.config.get("private"):
            raise ProviderError("The app was removed from this private channel. Add it again in Slack.", auth=True)
        self._call("conversations.join", channel=self.channel.external_id)
        return send()

    def _upload(self, item):
        data = item.read()
        params = {"filename": item.filename or "file", "length": len(data)}
        if item.alt and not item.is_video:
            params["alt_txt"] = item.alt[:1000]
        slot = self._call("files.getUploadURLExternal", form=True, **params)
        self.http("POST", slot["upload_url"], files={"file": (params["filename"], data, item.mime)})
        return slot["file_id"]

    def _shared_ts(self, file_id, channel_id, attempts=5):
        for i in range(attempts):
            try:
                info = self._call("files.info", http="GET", file=file_id)["file"]
            except ProviderError:
                return ""
            shares = info.get("shares") or {}
            for scope in ("public", "private"):
                found = (shares.get(scope) or {}).get(channel_id)
                if found:
                    return found[0].get("ts", "")
            if i < attempts - 1:
                time.sleep(1 + i)
        return ""

    def _permalink(self, channel_id, ts):
        try:
            return self._call("chat.getPermalink", http="GET", channel=channel_id, message_ts=ts).get("permalink", "")
        except ProviderError:
            return ""

    def delete(self, external_id):
        channel_id, _, rest = external_id.partition(":")
        if rest.startswith("files:"):
            for fid in rest[len("files:") :].split(","):
                self._call("files.delete", file=fid)
            return
        self._call("chat.delete", channel=channel_id, ts=rest)

    def fetch_audience(self):
        info = self._call(
            "conversations.info", http="GET", channel=self.channel.external_id, include_num_members="true"
        )
        return info["channel"].get("num_members")

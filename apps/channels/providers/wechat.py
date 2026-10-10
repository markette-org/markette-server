"""WeChat Official Accounts (公众号): articles pushed to followers or published as links.

Requirements on the WeChat side:
* a *verified* (认证) subscription or service account: unverified and personal
  accounts can only create drafts through the API;
* Markette's server IP in the account's API IP whitelist (基本配置 → IP白名单).

Flow: the cover image is uploaded as permanent material, body images through
media/uploadimg, the article is created with draft/add, then either
* message/mass/sendall (mode "push", default): delivered to all followers and shown
  in the account's history; counts against the account's mass-send quota; or
* freepublish/submit (mode "publish"): published with a permanent link but no push
  and not in the history list.
"""

import time

import markdown as md

from .base import BaseProvider, Candidate, Capabilities, CredentialField, ProviderError, PublishResult

API = "https://api.weixin.qq.com/cgi-bin"
TITLE_LIMIT = 64
DIGEST_LIMIT = 120
_token_cache: dict[str, tuple[float, str]] = {}


class WeChatProvider(BaseProvider):
    key = "wechat"
    label = "WeChat"
    capabilities = Capabilities(
        kind="blog",
        max_chars=None,
        max_media=20,
        requires_media=True,  # the cover image
        supports_video=False,
        supports_title=True,
        auth="credentials",
        has_metrics=False,
        metric_names=(),
    )
    credential_fields = [
        CredentialField("app_id", "AppID", help="Official Account → 设置与开发 → 基本配置 (Basic configuration)."),
        CredentialField("app_secret", "AppSecret", type="password"),
        CredentialField(
            "mode",
            "Publishing",
            type="select",
            default="push",
            choices=["push", "publish"],
            help="push: send to all followers (uses the mass-send quota, verified accounts). "
            "publish: permanent link without notifying followers.",
        ),
        CredentialField(
            "account_name", "Account name", required=False, help="Shown in Markette, e.g. your 公众号 name."
        ),
        CredentialField("author", "Author name", required=False),
    ]

    # --- API plumbing ----------------------------------------------------------------
    def _token(self, app_id=None, secret=None, force=False):
        app_id = app_id or self.creds["app_id"]
        secret = secret or self.creds["app_secret"]
        cached = _token_cache.get(app_id)
        if cached and not force and cached[0] > time.time() + 120:
            return cached[1]
        body = self.http(
            "POST",
            f"{API}/stable_token",
            json={
                "grant_type": "client_credential",
                "appid": app_id,
                "secret": secret,
                "force_refresh": False,
            },
        ).json()
        if "access_token" not in body:
            raise self._wx_error(body)
        _token_cache[app_id] = (time.time() + int(body.get("expires_in", 7200)), body["access_token"])
        return body["access_token"]

    def _wx_error(self, body):
        code, msg = body.get("errcode"), body.get("errmsg", "")
        hints = {
            40164: "Add this server's IP address to the Official Account's API IP whitelist.",
            48001: "This account is not allowed to use the API (only verified accounts can mass-send).",
            45028: "No mass-send quota left for this period.",
            40001: "The AppSecret is wrong or the access token expired.",
            40125: "The AppSecret is wrong.",
            40013: "The AppID is wrong.",
        }
        text = f"WeChat error {code}: {hints.get(code, msg)}"
        auth = code in (40001, 40125, 40013, 40164, 48001)
        retryable = code in (-1, 45009, 45028)
        return ProviderError(text, auth=auth, retryable=retryable, retry_after=3600 if code == 45028 else None)

    def _call(self, method, path, app_id=None, secret=None, **kwargs):
        for attempt in (0, 1):
            params = {**kwargs.pop("params", {}), "access_token": self._token(app_id, secret, force=attempt == 1)}
            body = self.http(method, f"{API}/{path}", params=params, **kwargs).json()
            code = body.get("errcode", 0)
            if code in (40001, 42001) and attempt == 0:
                continue  # token expired early: refresh once
            if code:
                raise self._wx_error(body)
            return body
        raise ProviderError("WeChat kept rejecting the access token.", auth=True)

    # --- connection --------------------------------------------------------------------
    def connect_with_credentials(self, data):
        app_id, secret = data["app_id"].strip(), data["app_secret"].strip()
        followers = self._call("GET", "user/get", app_id, secret).get("total")
        return [
            Candidate(
                external_id=app_id,
                name=data.get("account_name") or f"WeChat {app_id}",
                handle=app_id,
                credentials={"app_id": app_id, "app_secret": secret},
                config={"mode": data.get("mode") or "push", "author": (data.get("author") or "")[:16]},
                followers_count=followers,
            )
        ]

    # --- publishing -----------------------------------------------------------------------
    def validate(self, content):
        problems = super().validate(content)
        if len(content.title) > TITLE_LIMIT:
            problems.append(f"WeChat titles are limited to {TITLE_LIMIT} characters.")
        if content.media and content.media[0].mime not in ("image/jpeg", "image/png"):
            problems.append("The WeChat cover (first image) must be a JPEG or PNG.")
        return problems

    def _upload_material(self, item):
        return self._call(
            "POST",
            "material/add_material",
            params={"type": "image"},
            files={"media": (item.filename or "cover.jpg", item.read(), item.mime)},
        )["media_id"]

    def _upload_inline(self, item):
        return self._call(
            "POST", "media/uploadimg", files={"media": (item.filename or "image.jpg", item.read(), item.mime)}
        )["url"]

    def publish(self, content):
        cfg = self.channel.config
        mode = content.options.get("wechat_mode") or cfg.get("mode", "push")
        cover = content.media[0]
        thumb_id = self._upload_material(cover)
        body_md = content.text
        for item in content.media[1:]:
            body_md = body_md.replace(item.url, self._upload_inline(item))
        html = md.markdown(body_md, extensions=["extra", "sane_lists"])
        article = {
            "title": content.title[:TITLE_LIMIT],
            "author": cfg.get("author", "")[:16],
            "digest": (content.options.get("excerpt") or "")[:DIGEST_LIMIT],
            "content": html,
            "content_source_url": content.link or "",
            "thumb_media_id": thumb_id,
            "need_open_comment": 1 if content.options.get("open_comments", True) else 0,
        }
        media_id = self._call("POST", "draft/add", json={"articles": [article]})["media_id"]
        if mode == "publish":
            publish_id = self._call("POST", "freepublish/submit", json={"media_id": media_id})["publish_id"]
            url = self._wait_published(publish_id)
            return PublishResult(external_id=f"publish:{publish_id}", url=url)
        res = self._call(
            "POST",
            "message/mass/sendall",
            json={
                "filter": {"is_to_all": True},
                "mpnews": {"media_id": media_id},
                "msgtype": "mpnews",
                "send_ignore_reprint": 0,
            },
        )
        return PublishResult(external_id=f"mass:{res['msg_id']}", url="", extra={"msg_data_id": res.get("msg_data_id")})

    def _wait_published(self, publish_id):
        for _ in range(20):
            res = self._call("POST", "freepublish/get", json={"publish_id": publish_id})
            status = res.get("publish_status")
            if status == 0:
                items = (res.get("article_detail") or {}).get("item") or []
                return items[0].get("article_url", "") if items else ""
            if status not in (1,):  # 1 = publishing
                raise ProviderError(f"WeChat could not publish the article (status {status}).")
            time.sleep(3)
        return ""  # still processing: the article will appear; the link is not known yet

    def delete(self, external_id):
        kind, _, ident = external_id.partition(":")
        if kind == "mass":
            self._call("POST", "message/mass/delete", json={"msg_id": int(ident)})
        else:
            raise ProviderError("Delete published WeChat articles from the Official Account platform.")

    def fetch_audience(self):
        return self._call("GET", "user/get").get("total")

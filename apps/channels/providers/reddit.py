"""Reddit: text, link and image posts to subreddits and to the user's own profile.

OAuth2 "web app" with the scopes identity, submit, read, mysubreddits, flair and edit.
Since 2025 Reddit no longer creates API apps on demand: new apps go through the
Responsible Builder Policy (a Data Access Request reviewed by Reddit), and commercial
use needs Reddit's written approval. Reddit requires a descriptive User-Agent.

One channel per subreddit (moderated and subscribed ones are offered) or the profile
(``u_<name>``). Every subreddit has its own rules: flair may be required, link or text
posts may be disabled, and self-promotion is often limited. Reddit reports these as
errors, which Markette shows on the publication.
"""

import re
import time
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.utils import timezone

from apps.platform import conf

from .base import BaseProvider, Candidate, Capabilities, Metrics, ProviderError, PublishResult, redirect_uri
from .text import URL_RE

AUTHORIZE = "https://www.reddit.com/api/v1/authorize"
TOKEN = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
SCOPES = "identity submit read mysubreddits flair edit"
TITLE_MAX = 300
SR_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
REQUIREMENT_KEYS = (
    "is_flair_required",
    "title_text_min_length",
    "title_text_max_length",
    "title_required_strings",
    "title_blacklisted_strings",
    "title_regexes",
    "body_restriction_policy",
    "body_text_min_length",
    "body_text_max_length",
    "body_required_strings",
    "body_blacklisted_strings",
    "link_restriction_policy",
    "domain_whitelist",
    "domain_blacklist",
    "guidelines_text",
)
# "try again in 7 minutes" in RATELIMIT errors.
RATE_RE = re.compile(r"(\d+)\s+(second|minute)")


class RedditProvider(BaseProvider):
    key = "reddit"
    label = "Reddit"
    capabilities = Capabilities(
        max_chars=40000,
        max_media=1,
        supports_video=False,
        supports_title=True,
        supports_first_comment=True,
        first_comment_max=10000,
        metric_names=("likes", "comments", "shares"),
    )

    @classmethod
    def is_configured(cls):
        return bool(conf.get("REDDIT_CLIENT_ID") and conf.get("REDDIT_CLIENT_SECRET"))

    @property
    def user_agent(self):
        name = (self.channel.config.get("username") if self.channel else "") or "markette"
        return conf.get("REDDIT_USER_AGENT") or f"web:markette:1.0 (by /u/{name})"

    # --- connection -------------------------------------------------------------------
    def authorization_url(self, request):
        params = {
            "client_id": conf.get("REDDIT_CLIENT_ID"),
            "response_type": "code",
            "state": request.state,
            "redirect_uri": redirect_uri(self.key),
            "duration": "permanent",
            "scope": SCOPES,
        }
        return f"{AUTHORIZE}?{urlencode(params)}"

    def _token(self, data):
        tok = self.http(
            "POST",
            TOKEN,
            data=data,
            auth=(conf.get("REDDIT_CLIENT_ID"), conf.get("REDDIT_CLIENT_SECRET")),
            headers={"User-Agent": self.user_agent},
        ).json()
        if "access_token" not in tok:
            raise ProviderError(f"Reddit refused the token request: {tok.get('error', tok)}", auth=True)
        return tok, timezone.now() + timedelta(seconds=int(tok.get("expires_in", 3600)))

    def _get(self, token, path, **params):
        headers = {"Authorization": f"bearer {token}", "User-Agent": self.user_agent}
        return self.http("GET", f"{API}{path}", headers=headers, params={"raw_json": 1, **params}).json()

    def complete_oauth(self, request, params):
        tok, expires = self._token(
            {"grant_type": "authorization_code", "code": params["code"], "redirect_uri": redirect_uri(self.key)}
        )
        token = tok["access_token"]
        me = self._get(token, "/api/v1/me")
        username = me["name"]
        creds = {"access_token": token, "refresh_token": tok.get("refresh_token", "")}
        profile = (me.get("subreddit") or {}).get("display_name") or f"u_{username}"
        candidates = [
            Candidate(
                external_id=profile.lower(),
                name=f"u/{username} (profile)",
                handle=f"u/{username}",
                avatar_url=(me.get("icon_img") or "").split("?")[0],
                profile_url=f"https://www.reddit.com/user/{username}",
                credentials=creds,
                token_expires_at=expires,
                followers_count=(me.get("subreddit") or {}).get("subscribers"),
                config={"username": username, "subreddit": profile},
            )
        ]
        seen = {profile.lower()}
        for where in ("moderator", "subscriber"):
            listing = self._get(token, f"/subreddits/mine/{where}", limit=100)
            for child in (listing.get("data") or {}).get("children", []):
                sr = child["data"]
                name = sr["display_name"]
                if name.lower() in seen or sr.get("subreddit_type") == "user":
                    continue
                seen.add(name.lower())
                candidates.append(
                    Candidate(
                        external_id=name.lower(),
                        name=f"r/{name}",
                        handle=f"r/{name}" + (" · mod" if where == "moderator" else ""),
                        avatar_url=(sr.get("community_icon") or sr.get("icon_img") or "").split("?")[0],
                        profile_url=f"https://www.reddit.com/r/{name}/",
                        credentials=creds,
                        token_expires_at=expires,
                        followers_count=sr.get("subscribers"),
                        config={
                            "username": username,
                            "subreddit": name,
                            "moderator": where == "moderator",
                            "submission_type": sr.get("submission_type", "any"),
                            "nsfw": bool(sr.get("over18")),
                        },
                    )
                )
        return candidates

    def subreddit_candidate(self, name):
        """A channel for one specific subreddit, reusing this channel's Reddit account."""
        name = re.sub(r"^(?:https?://(?:www\.|old\.)?reddit\.com)?/?r/", "", name.strip()).strip("/").split("/")[0]
        if not SR_RE.match(name):
            raise ProviderError("Enter a subreddit name, e.g. r/python.")
        try:
            sr = self.api("GET", f"/r/{name}/about").get("data") or {}
        except ProviderError as exc:
            if exc.status in (403, 404):
                raise ProviderError(f"r/{name} does not exist, is private or is banned.") from exc
            raise
        if not sr.get("display_name"):
            raise ProviderError(f"r/{name} does not exist.")
        if sr.get("user_is_banned"):
            raise ProviderError(f"u/{self.channel.config.get('username')} is banned from r/{sr['display_name']}.")
        if sr.get("subreddit_type") in ("private", "restricted") and not (
            sr.get("user_is_contributor") or sr.get("user_is_moderator")
        ):
            raise ProviderError(f"Only approved users can post in r/{sr['display_name']}.")
        name = sr["display_name"]
        return Candidate(
            external_id=name.lower(),
            name=f"r/{name}",
            handle=f"r/{name}" + (" · mod" if sr.get("user_is_moderator") else ""),
            avatar_url=(sr.get("community_icon") or sr.get("icon_img") or "").split("?")[0],
            profile_url=f"https://www.reddit.com/r/{name}/",
            credentials=dict(self.creds),
            token_expires_at=self.channel.token_expires_at,
            followers_count=sr.get("subscribers"),
            config={
                "username": self.channel.config.get("username"),
                "subreddit": name,
                "moderator": bool(sr.get("user_is_moderator")),
                "submission_type": sr.get("submission_type", "any"),
                "nsfw": bool(sr.get("over18")),
                **self.subreddit_rules(name),
            },
        )

    def subreddit_rules(self, name):
        """Posting requirements and rule titles, checked before publishing and shown in the composer."""
        out = {}
        try:
            req = self.api("GET", f"/api/v1/{name}/post_requirements")
            out["requirements"] = {k: req[k] for k in REQUIREMENT_KEYS if req.get(k) not in (None, [], "", False)}
        except ProviderError as exc:
            if exc.status not in (403, 404):
                raise
        try:
            rules = self.api("GET", f"/r/{name}/about/rules").get("rules") or []
            out["rules"] = [r.get("short_name", "")[:200] for r in rules if r.get("kind") in ("all", "link", None)][:15]
        except ProviderError as exc:
            if exc.status not in (403, 404):
                raise
        return out

    def refresh_credentials(self):
        exp = self.channel.token_expires_at
        if exp and exp - timezone.now() > timedelta(minutes=10):
            return False
        if not self.creds.get("refresh_token"):
            raise ProviderError("Reddit access expired; reconnect the channel.", auth=True)
        tok, expires = self._token({"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"]})
        self.save_credentials({**self.creds, "access_token": tok["access_token"]}, expires)
        return True

    # --- API helpers ------------------------------------------------------------------
    @property
    def headers(self):
        return {"Authorization": f"bearer {self.creds['access_token']}", "User-Agent": self.user_agent}

    def api(self, method, path, **kwargs):
        if method == "GET":
            kwargs["params"] = {"raw_json": 1, **kwargs.get("params", {})}
        return self.http(method, f"{API}{path}", headers=self.headers, **kwargs).json()

    def error_from_response(self, resp):
        err = super().error_from_response(resp)
        if resp.status_code == 403:
            err.args = ("Reddit refused the request (banned, private subreddit, or missing permission).",)
        if resp.status_code == 429 or resp.headers.get("x-ratelimit-remaining") in ("0", "0.0"):
            err.retryable = True
            err.retry_after = err.retry_after or _seconds(resp.headers.get("x-ratelimit-reset")) or 60
        return err

    @staticmethod
    def check_errors(data):
        """Reddit answers 200 with ``json.errors`` for validation and rate-limit failures."""
        errors = (data.get("json") or {}).get("errors") or []
        if not errors:
            return
        code, message, *_ = errors[0] + [""] * 2
        text = f"Reddit: {message or code} ({code})"
        if code == "RATELIMIT":
            m = RATE_RE.search(message)
            wait = int(m.group(1)) * (60 if m.group(2) == "minute" else 1) if m else 600
            raise ProviderError(text, retryable=True, retry_after=wait + 5, payload=errors)
        if code in ("USER_REQUIRED", "INVALID_USER"):
            raise ProviderError(text, auth=True, payload=errors)
        raise ProviderError(text, payload=errors)

    # --- publishing -------------------------------------------------------------------
    def _post_type(self, content):
        if content.media:
            return "image"
        kind = content.options.get("post_type") or ("link" if content.link else "self")
        return kind if kind in ("self", "link") else "self"

    def _link(self, content):
        if content.link:
            return content.link
        urls = URL_RE.findall(content.text)
        return urls[0].rstrip(".,;:!?)") if urls else ""

    def validate(self, content):
        problems = super().validate(content)
        kind = self._post_type(content)
        if kind == "link" and self._link(content):
            problems = [p for p in problems if p != "Nothing to publish."]  # a bare link is a whole post
        if len(content.title) > TITLE_MAX:
            problems.append(f"Reddit titles are limited to {TITLE_MAX} characters.")
        allowed = self.channel.config.get("submission_type", "any") if self.channel else "any"
        if kind == "link" and not self._link(content):
            problems.append("A link post needs a link.")
        if kind == "link" and allowed == "self":
            problems.append("This subreddit only accepts text posts.")
        if kind == "self" and allowed == "link":
            problems.append("This subreddit only accepts link posts.")
        if self.channel is not None:
            problems += self.check_requirements(content, kind)
        return problems

    def check_requirements(self, content, kind):
        """The subreddit's own posting rules (``post_requirements``), as far as they can be checked here."""
        req = self.channel.config.get("requirements") or {}
        sr = f"r/{self.channel.config.get('subreddit') or self.channel.external_id}"
        title, body = content.title, content.text if kind != "link" else ""
        low_title, low_body = title.lower(), body.lower()
        problems = []
        if req.get("is_flair_required") and not (
            content.options.get("flair_id") or self.channel.config.get("flair_id")
        ):
            problems.append(f"{sr} requires a post flair.")
        if title and len(title) < (req.get("title_text_min_length") or 0):
            problems.append(f"{sr} needs titles of at least {req['title_text_min_length']} characters.")
        if req.get("title_text_max_length") and len(title) > req["title_text_max_length"]:
            problems.append(f"{sr} limits titles to {req['title_text_max_length']} characters.")
        if (need := req.get("title_required_strings")) and not any(x.lower() in low_title for x in need):
            problems.append(f"{sr} titles must contain one of: {', '.join(need)}.")
        if bad := [x for x in req.get("title_blacklisted_strings") or [] if x.lower() in low_title]:
            problems.append(f"{sr} does not allow these words in titles: {', '.join(bad)}.")
        for pattern in req.get("title_regexes") or []:
            try:
                if title and not re.search(pattern, title):
                    problems.append(f"{sr} titles must match the pattern {pattern}.")
            except re.error:
                pass
        if kind == "self":
            policy = req.get("body_restriction_policy")
            if policy == "required" and not body.strip():
                problems.append(f"{sr} requires text in the post body.")
            if policy == "notAllowed" and body.strip():
                problems.append(f"{sr} does not allow text in the post body.")
            if body and len(body) < (req.get("body_text_min_length") or 0):
                problems.append(f"{sr} needs a body of at least {req['body_text_min_length']} characters.")
            if req.get("body_text_max_length") and len(body) > req["body_text_max_length"]:
                problems.append(f"{sr} limits the body to {req['body_text_max_length']} characters.")
            if (need := req.get("body_required_strings")) and not any(x.lower() in low_body for x in need):
                problems.append(f"{sr} posts must contain one of: {', '.join(need)}.")
            if bad := [x for x in req.get("body_blacklisted_strings") or [] if x.lower() in low_body]:
                problems.append(f"{sr} does not allow these words: {', '.join(bad)}.")
        if kind == "link" and (link := self._link(content)):
            host = (urlsplit(link).hostname or "").lower().removeprefix("www.")
            listed = lambda domains: any(host == d or host.endswith("." + d) for d in domains)  # noqa: E731
            if req.get("link_restriction_policy") == "whitelist" and not listed(req.get("domain_whitelist") or []):
                problems.append(f"{sr} only accepts links to: {', '.join(req.get('domain_whitelist') or [])}.")
            if req.get("link_restriction_policy") == "blacklist" and listed(req.get("domain_blacklist") or []):
                problems.append(f"{sr} does not accept links to {host}.")
        return problems

    def prepare_first_comment(self, content):
        content, comment = super().prepare_first_comment(content)
        # A link post has no body: the text becomes the author's first comment.
        if self._post_type(content) == "link" and content.text.strip():
            link = self._link(content)
            body = content.text.replace(link, "").strip() if not content.link else content.text.strip()
            content.link = link
            content.text = ""
            comment = "\n\n".join(filter(None, [body, comment]))
        return content, comment

    def _upload_image(self, item):
        lease = self.api(
            "POST", "/api/media/asset.json", data={"filepath": item.filename or "image.jpg", "mimetype": item.mime}
        )
        args = lease["args"]
        action = args["action"]
        action = f"https:{action}" if action.startswith("//") else action
        fields = {f["name"]: f["value"] for f in args["fields"]}
        self.http(
            "POST",
            action,
            data=fields,
            files={"file": (item.filename or "image.jpg", item.read(), item.mime)},
            expect=(200, 201, 204),
        )
        return f"{action}/{fields['key']}"

    def _find_submitted(self, title):
        """Image posts are created asynchronously; find the new post among the user's submissions."""
        username = self.channel.config.get("username")
        sr = self.channel.config.get("subreddit", self.channel.external_id).lower()
        for _ in range(10):
            listing = self.api("GET", f"/user/{username}/submitted", params={"sort": "new", "limit": 10})
            for child in (listing.get("data") or {}).get("children", []):
                post = child["data"]
                if post.get("title") == title and post.get("subreddit", "").lower() == sr:
                    return post
            time.sleep(3)
        return None

    def publish(self, content):
        opts, cfg = content.options, self.channel.config
        sr = cfg.get("subreddit") or self.channel.external_id
        kind = self._post_type(content)
        title = content.title[:TITLE_MAX]
        data = {
            "api_type": "json",
            "sr": sr,
            "kind": kind,
            "title": title,
            "sendreplies": "true" if opts.get("send_replies", True) else "false",
            "resubmit": "true",
            "nsfw": "true" if opts.get("nsfw") else "false",
            "spoiler": "true" if opts.get("spoiler") else "false",
        }
        flair_id = opts.get("flair_id") or cfg.get("flair_id")
        if flair_id:
            data["flair_id"] = flair_id
            if opts.get("flair_text"):
                data["flair_text"] = opts["flair_text"][:64]
        if kind == "self":
            data["text"] = content.text
        elif kind == "link":
            data["url"] = self._link(content)
        else:
            data["url"] = self._upload_image(content.media[0])
            data["text"] = content.text  # shown as the post body where the subreddit allows it
        resp = self.api("POST", "/api/submit", data=data)
        self.check_errors(resp)
        result = (resp.get("json") or {}).get("data") or {}
        if result.get("name"):
            return PublishResult(external_id=result["name"], url=result.get("url", ""))
        post = self._find_submitted(title)
        if post is None:
            raise ProviderError("Reddit accepted the image post but did not confirm it; check the subreddit.")
        return PublishResult(external_id=post["name"], url=f"https://www.reddit.com{post['permalink']}")

    def post_first_comment(self, external_id, text):
        resp = self.api("POST", "/api/comment", data={"api_type": "json", "thing_id": external_id, "text": text})
        self.check_errors(resp)
        things = ((resp.get("json") or {}).get("data") or {}).get("things") or [{}]
        return (things[0].get("data") or {}).get("name", "")

    def delete(self, external_id):
        self.api("POST", "/api/del", data={"id": external_id})

    def flairs(self):
        """Post flairs the user may pick in this subreddit (empty when flair is disabled)."""
        sr = self.channel.config.get("subreddit") or self.channel.external_id
        try:
            items = self.api("GET", f"/r/{sr}/api/link_flair_v2")
        except ProviderError as exc:
            if exc.status in (403, 404):
                return []
            raise
        return [
            {"id": f["id"], "text": f.get("text", ""), "editable": bool(f.get("text_editable"))}
            for f in items or []
            if not f.get("mod_only") or self.channel.config.get("moderator")
        ]

    # --- metrics ------------------------------------------------------------------------
    def fetch_metrics(self, external_ids):
        out = {}
        for i in range(0, len(external_ids), 100):
            listing = self.api("GET", "/api/info", params={"id": ",".join(external_ids[i : i + 100])})
            for child in (listing.get("data") or {}).get("children", []):
                p = child["data"]
                out[p["name"]] = Metrics(
                    likes=p.get("score"),
                    comments=p.get("num_comments"),
                    shares=p.get("num_crossposts"),
                    video_views=p.get("view_count"),
                    raw={k: p.get(k) for k in ("score", "ups", "upvote_ratio", "num_comments", "num_crossposts")},
                )
        return out

    def fetch_audience(self):
        sr = self.channel.config.get("subreddit") or self.channel.external_id
        about = self.api("GET", f"/r/{sr}/about")
        if not sr.lower().startswith("u_"):
            # Sync also refreshes the subreddit's rules; the caller saves the channel.
            self.channel.config = {**self.channel.config, **self.subreddit_rules(sr)}
        return (about.get("data") or {}).get("subscribers")


def _seconds(value):
    try:
        return int(float(value)) + 1 if value else None
    except ValueError:
        return None

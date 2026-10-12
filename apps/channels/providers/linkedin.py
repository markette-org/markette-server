"""LinkedIn members and organization pages via the versioned REST Posts API."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from django.utils import timezone

from apps.platform import conf

from .base import (
    BaseProvider,
    Candidate,
    Capabilities,
    HistoryEntry,
    InboxEntry,
    Metrics,
    ProviderError,
    PublishResult,
    redirect_uri,
    repeatable,
)

AUTHORIZE = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"
REST = "https://api.linkedin.com/rest"
MEMBER_SCOPES = ["openid", "profile", "w_member_social"]
ORG_SCOPES = ["r_organization_social", "w_organization_social", "rw_organization_admin"]


class LinkedInProvider(BaseProvider):
    key = "linkedin"
    label = "LinkedIn"
    capabilities = Capabilities(
        max_chars=3000,
        supports_edit=True,
        history=True,
        # Comments on company page posts (organization scopes); personal profiles cannot read them.
        inbox=True,
        inbox_interval=15,
        inbox_reply=True,
        max_media=9,
        supports_first_comment=True,
        first_comment_max=1250,
        metric_names=("impressions", "likes", "comments", "shares", "clicks"),
    )

    @classmethod
    def is_configured(cls):
        return bool(conf.get("LINKEDIN_CLIENT_ID") and conf.get("LINKEDIN_CLIENT_SECRET"))

    @staticmethod
    def scopes():
        return MEMBER_SCOPES + (ORG_SCOPES if conf.get("LINKEDIN_ORG_SCOPES") else [])

    def authorization_url(self, request):
        params = {
            "response_type": "code",
            "client_id": conf.get("LINKEDIN_CLIENT_ID"),
            "redirect_uri": redirect_uri(self.key),
            "state": request.state,
            "scope": " ".join(self.scopes()),
        }
        return f"{AUTHORIZE}?{urlencode(params)}"

    def _token(self, data):
        tok = self.http(
            "POST",
            TOKEN,
            data={
                **data,
                "client_id": conf.get("LINKEDIN_CLIENT_ID"),
                "client_secret": conf.get("LINKEDIN_CLIENT_SECRET"),
            },
        ).json()
        creds = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", "")}
        if tok.get("refresh_token_expires_in"):
            creds["refresh_expires_at"] = (
                timezone.now() + timedelta(seconds=tok["refresh_token_expires_in"])
            ).isoformat()
        return creds, timezone.now() + timedelta(seconds=int(tok.get("expires_in", 5_184_000)))

    def _headers(self, token):
        return {
            "Authorization": f"Bearer {token}",
            "LinkedIn-Version": conf.get("LINKEDIN_API_VERSION"),
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def complete_oauth(self, request, params):
        creds, expires = self._token(
            {
                "grant_type": "authorization_code",
                "code": params["code"],
                "redirect_uri": redirect_uri(self.key),
            }
        )
        token = creds["access_token"]
        me = self.http(
            "GET", "https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {token}"}
        ).json()
        person = f"urn:li:person:{me['sub']}"
        candidates = [
            Candidate(
                external_id=person,
                name=me.get("name") or f"{me.get('given_name', '')} {me.get('family_name', '')}".strip(),
                avatar_url=me.get("picture", ""),
                credentials={**creds, "author": person},
                config={"author_type": "person"},
                token_expires_at=expires,
            )
        ]
        if conf.get("LINKEDIN_ORG_SCOPES"):
            candidates += self._organizations(token, creds, expires)
        return candidates

    def _organizations(self, token, creds, expires):
        acls = (
            self.http(
                "GET",
                f"{REST}/organizationAcls",
                headers=self._headers(token),
                params={
                    "q": "roleAssignee",
                    "role": "ADMINISTRATOR",
                    "state": "APPROVED",
                },
            )
            .json()
            .get("elements", [])
        )
        out = []
        for acl in acls:
            org_urn = acl["organization"]
            org_id = org_urn.rsplit(":", 1)[-1]
            try:
                org = self.http("GET", f"{REST}/organizations/{org_id}", headers=self._headers(token)).json()
            except ProviderError:
                org = {}
            vanity = org.get("vanityName", "")
            out.append(
                Candidate(
                    external_id=org_urn,
                    name=org.get("localizedName") or org_urn,
                    handle=vanity,
                    profile_url=f"https://www.linkedin.com/company/{vanity or org_id}",
                    credentials={**creds, "author": org_urn},
                    config={"author_type": "organization"},
                    token_expires_at=expires,
                )
            )
        return out

    def refresh_credentials(self):
        exp = self.channel.token_expires_at
        if exp and exp - timezone.now() > timedelta(days=7):
            return False
        if not self.creds.get("refresh_token"):
            if exp and exp < timezone.now():
                raise ProviderError("LinkedIn token expired; reconnect the channel.", auth=True)
            return False
        creds, expires = self._token({"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"]})
        self.save_credentials({**self.creds, **creds}, expires)
        return True

    @property
    def headers(self):
        return self._headers(self.creds["access_token"])

    @property
    def author(self):
        return self.creds.get("author") or self.channel.external_id

    # --- publishing ------------------------------------------------------
    @repeatable
    def _upload(self, item):
        kind = "videos" if item.is_video else "images"
        if item.is_video:
            init = self.http(
                "POST",
                f"{REST}/videos?action=initializeUpload",
                headers=self.headers,
                json={
                    "initializeUploadRequest": {
                        "owner": self.author,
                        "fileSizeBytes": item.size,
                        "uploadCaptions": False,
                        "uploadThumbnail": False,
                    },
                },
            ).json()["value"]
            etags = []
            with item.path.open("rb") as fh:
                for instr in init["uploadInstructions"]:
                    fh.seek(instr["firstByte"])
                    chunk = fh.read(instr["lastByte"] - instr["firstByte"] + 1)
                    r = self.http(
                        "PUT", instr["uploadUrl"], content=chunk, headers={"Content-Type": "application/octet-stream"}
                    )
                    etags.append(r.headers.get("etag", "").strip('"'))
            self.http(
                "POST",
                f"{REST}/videos?action=finalizeUpload",
                headers=self.headers,
                json={
                    "finalizeUploadRequest": {
                        "video": init["video"],
                        "uploadToken": init.get("uploadToken", ""),
                        "uploadedPartIds": etags,
                    },
                },
            )
            return init["video"]
        init = self.http(
            "POST",
            f"{REST}/{kind}?action=initializeUpload",
            headers=self.headers,
            json={
                "initializeUploadRequest": {"owner": self.author},
            },
        ).json()["value"]
        self.http(
            "PUT",
            init["uploadUrl"],
            content=item.read(),
            headers={"Authorization": self.headers["Authorization"], "Content-Type": item.mime},
        )
        return init["image"]

    def publish(self, content):
        body = {
            "author": self.author,
            "commentary": _escape_little_text(content.text),
            "visibility": content.options.get("visibility", "PUBLIC"),
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if content.media:
            urns = [(self._upload(m), m) for m in content.media]
            if len(urns) == 1:
                urn, m = urns[0]
                body["content"] = {"media": {"id": urn, "altText": m.alt[:4086], "title": content.title[:400]}}
            else:
                body["content"] = {"multiImage": {"images": [{"id": u, "altText": m.alt[:4086]} for u, m in urns]}}
        elif content.link:
            body["content"] = {"article": {"source": content.link, "title": content.title or content.link}}
        resp = self.http("POST", f"{REST}/posts", headers=self.headers, json=body)
        urn = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id", "")
        return PublishResult(external_id=urn, url=f"https://www.linkedin.com/feed/update/{urn}/")

    @property
    def duplicate_guard(self):
        # Only organization pages can read their own posts back (r_organization_social);
        # LinkedIn grants no such permission for personal profiles.
        return "lookup" if self.author.startswith("urn:li:organization:") else None

    def find_published(self, content, since):
        posts = self.http(
            "GET",
            f"{REST}/posts?q=author&author={quote(self.author, safe='')}&count=20&sortBy=LAST_MODIFIED",
            headers={**self.headers, "X-RestLi-Method": "FINDER"},
        ).json()
        commentary = _escape_little_text(content.text)
        for p in posts.get("elements", []):
            if p.get("commentary") == commentary and (p.get("createdAt") or 0) >= since.timestamp() * 1000:
                return PublishResult(external_id=p["id"], url=f"https://www.linkedin.com/feed/update/{p['id']}/")
        return None

    def update(self, external_id, content, extra):
        self.http(
            "POST",
            f"{REST}/posts/{quote(external_id, safe='')}",
            headers={**self.headers, "X-RestLi-Method": "PARTIAL_UPDATE"},
            json={"patch": {"$set": {"commentary": _escape_little_text(content.text)}}},
        )

    def fetch_history(self, limit=100):
        out, start = [], 0
        while len(out) < limit:
            res = self.http(
                "GET",
                f"{REST}/posts?q=author&author={quote(self.author, safe='')}&count=50&start={start}&sortBy=LAST_MODIFIED",
                headers={**self.headers, "X-RestLi-Method": "FINDER"},
            ).json()
            elements = res.get("elements", [])
            for p in elements:
                when = p.get("publishedAt") or p.get("createdAt") or 0
                out.append(
                    HistoryEntry(
                        external_id=p["id"],
                        published_at=datetime.fromtimestamp(when / 1000, tz=UTC),
                        text=p.get("commentary", ""),
                        url=f"https://www.linkedin.com/feed/update/{p['id']}/",
                    )
                )
            if len(elements) < 50:
                break
            start += 50
        return out[:limit]

    def fetch_inbox(self, state, recent_posts):
        if not self.author.startswith("urn:li:organization:"):
            return [], state
        entries = []
        for urn in recent_posts[:10]:
            res = self.http(
                "GET",
                f"{REST}/socialActions/{quote(urn, safe='')}/comments",
                headers=self.headers,
                params={"count": 50},
            ).json()
            for c in res.get("elements", []):
                if c.get("actor") == self.author:
                    continue
                cid = c.get("commentUrn") or c.get("$URN") or f"urn:li:comment:({urn},{c.get('id')})"
                entries.append(
                    InboxEntry(
                        external_id=cid,
                        kind="comment",
                        text=(c.get("message") or {}).get("text", ""),
                        url=f"https://www.linkedin.com/feed/update/{urn}/",
                        author_name=c.get("actor", ""),
                        created_at=datetime.fromtimestamp(((c.get("created") or {}).get("time") or 0) / 1000, tz=UTC),
                        post_external_id=urn,
                        context={"object": urn},
                    )
                )
        return entries, state

    def reply_inbox(self, entry, text):
        obj = entry.context.get("object", "")
        resp = self.http(
            "POST",
            f"{REST}/socialActions/{quote(entry.external_id, safe='')}/comments",
            headers=self.headers,
            json={"actor": self.author, "object": obj, "parentComment": entry.external_id, "message": {"text": text}},
        )
        return PublishResult(
            external_id=resp.headers.get("x-restli-id", ""), url=f"https://www.linkedin.com/feed/update/{obj}/"
        )

    def post_first_comment(self, external_id, text):
        resp = self.http(
            "POST",
            f"{REST}/socialActions/{quote(external_id, safe='')}/comments",
            headers=self.headers,
            json={"actor": self.author, "object": external_id, "message": {"text": text}},
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.headers.get("x-restli-id") or body.get("id") or body.get("$URN", "")

    def delete(self, external_id):
        self.http("DELETE", f"{REST}/posts/{quote(external_id, safe='')}", headers=self.headers)

    # --- metrics ---------------------------------------------------------
    def fetch_metrics(self, external_ids):
        out = {}
        for urn in external_ids:
            try:
                social = self.http("GET", f"{REST}/socialMetadata/{quote(urn, safe='')}", headers=self.headers).json()
            except ProviderError as exc:
                if exc.status in (403, 404):
                    continue
                raise
            reactions = sum(v.get("count", 0) for v in (social.get("reactionSummaries") or {}).values())
            comments = (social.get("commentSummary") or {}).get("count")
            out[urn] = Metrics(likes=reactions, comments=comments, raw={"socialMetadata": social})
        if self.channel.config.get("author_type") == "organization" and external_ids:
            self._org_stats(external_ids, out)
        return out

    def _org_stats(self, urns, out):
        shares = [u for u in urns if u.startswith("urn:li:share:")]
        ugc = [u for u in urns if u.startswith("urn:li:ugcPost:")]
        params = [("q", "organizationalEntity"), ("organizationalEntity", self.author)]
        if shares:
            params.append(("shares", "List(" + ",".join(quote(u, safe="") for u in shares) + ")"))
        if ugc:
            params.append(("ugcPosts", "List(" + ",".join(quote(u, safe="") for u in ugc) + ")"))
        query = "&".join(f"{k}={v if k in ('shares', 'ugcPosts') else quote(v, safe='')}" for k, v in params)
        try:
            data = self.http("GET", f"{REST}/organizationalEntityShareStatistics?{query}", headers=self.headers).json()
        except ProviderError:
            return
        for el in data.get("elements", []):
            urn = el.get("share") or el.get("ugcPost")
            stats = el.get("totalShareStatistics", {})
            m = out.setdefault(urn, Metrics())
            m.impressions = stats.get("impressionCount")
            m.reach = stats.get("uniqueImpressionsCount")
            m.clicks = stats.get("clickCount")
            m.shares = stats.get("shareCount")
            m.likes = stats.get("likeCount", m.likes)
            m.comments = stats.get("commentCount", m.comments)
            m.raw["shareStatistics"] = stats

    def fetch_audience(self):
        if self.channel.config.get("author_type") != "organization":
            return None
        data = self.http(
            "GET",
            f"{REST}/networkSizes/{quote(self.author, safe='')}",
            headers=self.headers,
            params={"edgeType": "COMPANY_FOLLOWED_BY_MEMBER"},
        ).json()
        return data.get("firstDegreeSize")


def _escape_little_text(text):
    """LinkedIn commentary uses 'little text' markup; reserved characters must be escaped."""
    out = []
    for ch in text:
        if ch in "\\|{}@[]()<>#*_~":
            out.append("\\" + ch)
        else:
            out.append(ch)
    # Keep hashtags working: LinkedIn turns "#tag" into a hashtag only when '#' is unescaped.
    return re.sub(r"\\#(\w)", r"#\1", "".join(out))

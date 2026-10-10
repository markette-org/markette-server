"""Bluesky via AT Protocol XRPC, authenticated with an app password."""

import re
from datetime import UTC, datetime

from .base import BaseProvider, Candidate, Capabilities, CredentialField, Metrics, ProviderError, PublishResult
from .text import HASHTAG_RE, URL_RE

DEFAULT_SERVICE = "https://bsky.social"
APPVIEW = "https://public.api.bsky.app"
MENTION_RE = re.compile(r"(?<![\w@])@([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)")
MAX_IMAGE_BYTES = 2_000_000


def _byte_span(text, start, end):
    return len(text[:start].encode()), len(text[:end].encode())


class BlueskyProvider(BaseProvider):
    key = "bluesky"
    label = "Bluesky"
    capabilities = Capabilities(
        max_chars=300,
        count_mode="graphemes",
        max_media=4,
        auth="credentials",
        metric_names=("likes", "comments", "shares"),
    )
    credential_fields = [
        CredentialField("identifier", "Handle or email", help="e.g. you.bsky.social"),
        CredentialField(
            "app_password",
            "App password",
            type="password",
            help="Create one in Bluesky → Settings → Privacy and security → App passwords.",
        ),
        CredentialField("service", "PDS / entryway", type="url", required=False, default=DEFAULT_SERVICE),
    ]

    # --- auth ------------------------------------------------------------
    def connect_with_credentials(self, data):
        service = (data.get("service") or DEFAULT_SERVICE).rstrip("/")
        sess = self.http(
            "POST",
            f"{service}/xrpc/com.atproto.server.createSession",
            json={
                "identifier": data["identifier"].strip().lstrip("@"),
                "password": data["app_password"].strip(),
            },
        ).json()
        pds = _pds_from_did_doc(sess.get("didDoc")) or service
        profile = self.http("GET", f"{APPVIEW}/xrpc/app.bsky.actor.getProfile", params={"actor": sess["did"]}).json()
        return [
            Candidate(
                external_id=sess["did"],
                name=profile.get("displayName") or sess["handle"],
                handle=f"@{sess['handle']}",
                avatar_url=profile.get("avatar", ""),
                profile_url=f"https://bsky.app/profile/{sess['handle']}",
                base_url=pds,
                credentials={
                    "identifier": data["identifier"].strip().lstrip("@"),
                    "app_password": data["app_password"].strip(),
                    "service": service,
                    "access_jwt": sess["accessJwt"],
                    "refresh_jwt": sess["refreshJwt"],
                },
                followers_count=profile.get("followersCount"),
            )
        ]

    def _session(self, force=False):
        """Return a valid access JWT, refreshing or recreating the session if needed."""
        creds = dict(self.creds)
        if not force and creds.get("access_jwt"):
            return creds["access_jwt"]
        try:
            sess = self.http(
                "POST",
                f"{self.pds}/xrpc/com.atproto.server.refreshSession",
                headers={"Authorization": f"Bearer {creds.get('refresh_jwt', '')}"},
            ).json()
        except ProviderError:
            try:
                sess = self.http(
                    "POST",
                    f"{creds.get('service', DEFAULT_SERVICE)}/xrpc/com.atproto.server.createSession",
                    json={"identifier": creds["identifier"], "password": creds["app_password"]},
                ).json()
            except ProviderError as exc:
                raise ProviderError(f"Bluesky login failed: {exc}", auth=True) from exc
        creds.update(access_jwt=sess["accessJwt"], refresh_jwt=sess["refreshJwt"])
        self.save_credentials(creds)
        return creds["access_jwt"]

    def refresh_credentials(self):
        self._session(force=True)
        return True

    @property
    def pds(self):
        return (self.channel.base_url or DEFAULT_SERVICE).rstrip("/")

    def _xrpc(self, method, nsid, **kwargs):
        for attempt in (0, 1):
            headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self._session(force=attempt == 1)}"}
            try:
                return self.http(method, f"{self.pds}/xrpc/{nsid}", headers=headers, **kwargs)
            except ProviderError as exc:
                expired = exc.status in (400, 401) and "ExpiredToken" in str(exc.payload)
                if attempt == 0 and (exc.auth or expired):
                    continue
                raise

    # --- publishing ------------------------------------------------------
    def facets(self, text):
        facets = []
        for m in URL_RE.finditer(text):
            url = m.group(0).rstrip(".,;:!?)")
            s, e = _byte_span(text, m.start(), m.start() + len(url))
            facets.append(
                {
                    "index": {"byteStart": s, "byteEnd": e},
                    "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
                }
            )
        for m in HASHTAG_RE.finditer(text):
            s, e = _byte_span(text, m.start(), m.end())
            facets.append(
                {
                    "index": {"byteStart": s, "byteEnd": e},
                    "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}],
                }
            )
        for m in MENTION_RE.finditer(text):
            try:
                did = self.http(
                    "GET", f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle", params={"handle": m.group(1)}
                ).json()["did"]
            except ProviderError:
                continue
            s, e = _byte_span(text, m.start(), m.end())
            facets.append(
                {
                    "index": {"byteStart": s, "byteEnd": e},
                    "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}],
                }
            )
        return facets

    def _upload_blob(self, item):
        if item.size > MAX_IMAGE_BYTES and not item.is_video:
            raise ProviderError(f"Bluesky images must be under 2 MB ({item.filename} is {item.size} bytes).")
        return self._xrpc(
            "POST", "com.atproto.repo.uploadBlob", content=item.read(), headers={"Content-Type": item.mime}
        ).json()["blob"]

    def publish(self, content):
        record = {
            "$type": "app.bsky.feed.post",
            "text": content.text,
            "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        if langs := content.options.get("langs"):
            record["langs"] = langs
        if facets := self.facets(content.text):
            record["facets"] = facets
        videos = [m for m in content.media if m.is_video]
        if videos:
            v = videos[0]
            embed = {"$type": "app.bsky.embed.video", "video": self._upload_blob(v), "alt": v.alt[:1000]}
            if v.width and v.height:
                embed["aspectRatio"] = {"width": v.width, "height": v.height}
            record["embed"] = embed
        elif content.media:
            images = []
            for m in content.media[:4]:
                img = {"image": self._upload_blob(m), "alt": m.alt[:2000]}
                if m.width and m.height:
                    img["aspectRatio"] = {"width": m.width, "height": m.height}
                images.append(img)
            record["embed"] = {"$type": "app.bsky.embed.images", "images": images}
        res = self._xrpc(
            "POST",
            "com.atproto.repo.createRecord",
            json={
                "repo": self.channel.external_id,
                "collection": "app.bsky.feed.post",
                "record": record,
            },
        ).json()
        uri = res["uri"]
        rkey = uri.rsplit("/", 1)[-1]
        handle = (self.channel.handle or self.channel.external_id).lstrip("@")
        return PublishResult(
            external_id=uri, url=f"https://bsky.app/profile/{handle}/post/{rkey}", extra={"cid": res.get("cid")}
        )

    def delete(self, external_id):
        rkey = external_id.rsplit("/", 1)[-1]
        self._xrpc(
            "POST",
            "com.atproto.repo.deleteRecord",
            json={
                "repo": self.channel.external_id,
                "collection": "app.bsky.feed.post",
                "rkey": rkey,
            },
        )

    # --- metrics ---------------------------------------------------------
    def fetch_metrics(self, external_ids):
        out = {}
        for i in range(0, len(external_ids), 25):
            batch = external_ids[i : i + 25]
            posts = self.http("GET", f"{APPVIEW}/xrpc/app.bsky.feed.getPosts", params={"uris": batch}).json()["posts"]
            for p in posts:
                out[p["uri"]] = Metrics(
                    likes=p.get("likeCount"),
                    comments=p.get("replyCount"),
                    shares=(p.get("repostCount") or 0) + (p.get("quoteCount") or 0),
                    raw={k: p.get(k) for k in ("likeCount", "replyCount", "repostCount", "quoteCount")},
                )
        return out

    def fetch_audience(self):
        prof = self.http(
            "GET", f"{APPVIEW}/xrpc/app.bsky.actor.getProfile", params={"actor": self.channel.external_id}
        ).json()
        return prof.get("followersCount")


def _pds_from_did_doc(doc):
    for svc in (doc or {}).get("service", []):
        if svc.get("id") in ("#atproto_pds", f"{doc.get('id')}#atproto_pds"):
            return svc.get("serviceEndpoint", "").rstrip("/")
    return None

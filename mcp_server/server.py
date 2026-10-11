"""Markette MCP server.

Exposes Markette to AI assistants (Claude Desktop/Code, Cursor, ChatGPT connectors, …)
over the Model Context Protocol. It is a thin layer over the REST API: every tool
call is made *as the user* whose personal API token (``mkt_…``) authenticates the
MCP request, so workspace roles, team channel access and approval rules all apply.

Run:
    uvicorn mcp_server.server:app --host 0.0.0.0 --port 8001     # Streamable HTTP at /mcp
    MARKETTE_TOKEN=mkt_… python -m mcp_server                      # stdio, for local clients

Environment:
    MARKETTE_API_URL   base URL of the Markette API (default http://api:8000)
    PUBLIC_MCP_URL     public URL of this endpoint (for OAuth protected-resource metadata)
    PUBLIC_API_URL     public URL of the API (advertised as the token issuer)
"""

from __future__ import annotations

import os
from typing import Any, Literal

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations

API_URL = os.environ.get("MARKETTE_API_URL", "http://api:8000").rstrip("/")
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL", "http://localhost:8000").rstrip("/")
PUBLIC_MCP_URL = os.environ.get("PUBLIC_MCP_URL", f"{PUBLIC_API_URL}/mcp")
# Host header the API expects when called through the internal network.
API_HOST = os.environ.get("MARKETTE_API_HOST", "")
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
PUBLISH = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def _token() -> str:
    access = get_access_token()
    if access is not None:
        return access.token
    token = os.environ.get("MARKETTE_TOKEN", "")
    if not token:
        raise ToolError("Not authenticated: send 'Authorization: Bearer mkt_…' or set MARKETTE_TOKEN.")
    return token


async def api(method: str, path: str, *, workspace: str | None = None, **kwargs) -> Any:
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    if workspace:
        headers["X-Workspace"] = workspace
    if API_HOST:
        headers["Host"] = API_HOST
    async with httpx.AsyncClient(base_url=API_URL, timeout=60.0) as client:
        resp = await client.request(method, f"/api/{path.lstrip('/')}", headers=headers, **kwargs)
    if resp.status_code == 204:
        return None
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    if resp.status_code >= 400:
        raise ToolError(f"Markette API error {resp.status_code}: {_message(data)}")
    if resp.status_code == 202 and isinstance(data, dict):
        data["_note"] = "Submitted for review: a team reviewer must approve before it is scheduled."
    return data


def _message(data) -> str:
    if isinstance(data, dict):
        if "detail" in data:
            return str(data["detail"])
        return "; ".join(f"{k}: {v}" for k, v in data.items())[:800]
    return str(data)[:800]


def _results(data):
    return data["results"] if isinstance(data, dict) and "results" in data else data


class MarketteTokenVerifier:
    """Accepts a Markette personal API token by asking the API who it belongs to."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token.startswith("mkt_"):
            return None
        headers = {"Authorization": f"Bearer {token}"}
        if API_HOST:
            headers["Host"] = API_HOST
        try:
            async with httpx.AsyncClient(base_url=API_URL, timeout=15.0) as client:
                resp = await client.get("/api/auth/me/", headers=headers)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        me = resp.json()
        return AccessToken(
            token=token,
            client_id="markette-api-token",
            scopes=["markette"],
            subject=str(me["id"]),
            resource=PUBLIC_MCP_URL,
            claims={"email": me["email"]},
        )


mcp = MCPServer(
    name="markette",
    title="Markette",
    instructions=(
        "Markette publishes and schedules posts to social networks (X, Bluesky, Threads, Mastodon, LinkedIn, "
        "Facebook, Instagram, Telegram, Slack, Discord…) and sites (WordPress, Drupal, Ghost, Medium, DEV, Hugo), "
        "and reports their metrics. Start with "
        "list_workspaces, then pass the workspace slug to the other tools. Create posts as drafts with "
        "create_post, check them with validate_post, then schedule_post. Times are ISO 8601; when the user "
        "gives a local time, use the workspace time zone returned by list_workspaces. Scheduling may return "
        "'pending' when a team requires approval. To announce a publication elsewhere (e.g. a blog article on "
        "X), create the announcement with follows_target_id: it waits for that publication and links to it."
    ),
    website_url=PUBLIC_API_URL,
    token_verifier=MarketteTokenVerifier(),
    auth=AuthSettings(issuer_url=PUBLIC_API_URL, resource_server_url=PUBLIC_MCP_URL, validate_token_resource=True),
)


# --- workspaces & channels -----------------------------------------------------------


@mcp.tool(annotations=READ)
async def list_workspaces() -> list[dict]:
    """List the workspaces you can use, with organization, your role and time zone."""
    rows = _results(await api("GET", "workspaces/"))
    return [
        {k: w.get(k) for k in ("slug", "name", "organization_name", "role", "timezone", "is_archived")} for w in rows
    ]


@mcp.tool(annotations=READ)
async def list_channels(workspace: str) -> list[dict]:
    """List the publishing channels (social accounts, blogs, sites) you can use in a workspace."""
    rows = _results(await api("GET", "channels/", workspace=workspace, params={"limit": 200}))
    return [
        {k: c.get(k) for k in ("id", "provider", "name", "handle", "status", "followers_count", "approval_required")}
        for c in rows
    ]


# --- posts ---------------------------------------------------------------------------


def _post_summary(p: dict) -> dict:
    return {
        "id": p["id"],
        "title": p.get("title"),
        "body": p.get("body"),
        "status": p.get("status"),
        "approval_status": p.get("approval_status") or None,
        "targets": [
            {
                "id": t["id"],
                "channel_id": t["channel"],
                "channel": (t.get("channel_detail") or {}).get("name"),
                "status": t["status"],
                "scheduled_at": t.get("scheduled_at"),
                "published_at": t.get("published_at"),
                "url": t.get("external_url") or None,
                "error": t.get("last_error") or None,
                "metrics": t.get("metrics"),
                **(
                    {
                        "follows": {
                            "target_id": t["follows"],
                            "channel": (t.get("follows_detail") or {}).get("channel_name"),
                            "post_id": (t.get("follows_detail") or {}).get("post_id"),
                        },
                        "waiting": t.get("waiting", False),
                    }
                    if t.get("follows")
                    else {}
                ),
            }
            for t in p.get("targets", [])
        ],
    }


@mcp.tool(annotations=READ)
async def list_posts(
    workspace: str,
    status: str | None = None,
    search: str | None = None,
    awaiting_my_review: bool = False,
    limit: int = 20,
) -> list[dict]:
    """List recent posts. status: draft, scheduled, publishing, published, partial or failed (comma-separated)."""
    params: dict[str, Any] = {"limit": min(max(limit, 1), 100)}
    if status:
        params["status"] = status
    if search:
        params["search"] = search
    if awaiting_my_review:
        params["review"] = "mine"
    return [_post_summary(p) for p in _results(await api("GET", "posts/", workspace=workspace, params=params))]


@mcp.tool(annotations=READ)
async def get_post(workspace: str, post_id: int) -> dict:
    """Get one post with each channel's status, schedule, link and latest metrics."""
    return _post_summary(await api("GET", f"posts/{post_id}/", workspace=workspace))


@mcp.tool(annotations=WRITE)
async def create_post(
    workspace: str,
    body: str,
    channel_ids: list[int],
    title: str = "",
    link: str = "",
    tags: list[str] | None = None,
    channel_overrides: dict[str, str] | None = None,
    follows_target_id: int | None = None,
    follow_mode: Literal["delay", "queue", "best_time"] = "delay",
    follow_delay_minutes: int = 0,
) -> dict:
    """Create a draft post for the given channels (nothing is published yet).

    title is required for blog channels (WordPress, Drupal, Medium, Hugo, DEV); body is Markdown for them.
    channel_overrides maps a channel id to a channel-specific text (e.g. a shorter version for X).

    To announce another publication (e.g. a blog article on X and Bluesky), pass its target id
    (from get_post or list_posts) as follows_target_id: once scheduled, every channel of this post
    waits until that publication is live, then goes out with its URL in place of {url} (and its
    title in place of {title}); without {url} the URL is added at the end. follow_mode picks when:
    "delay" (follow_delay_minutes after it), "queue" (next queue slot after it) or "best_time".
    """
    overrides = channel_overrides or {}
    follow = (
        {"follows": follows_target_id, "follow_mode": follow_mode, "follow_delay": follow_delay_minutes}
        if follows_target_id
        else {}
    )
    payload = {
        "title": title,
        "body": body,
        "link": link,
        "tags": tags or [],
        "targets": [{"channel": cid, "body": overrides.get(str(cid), ""), **follow} for cid in channel_ids],
    }
    return _post_summary(await api("POST", "posts/", workspace=workspace, json=payload))


@mcp.tool(annotations=READ)
async def validate_post(workspace: str, post_id: int) -> dict:
    """Check a post against each channel's rules (length, media, title) before scheduling."""
    problems = await api("GET", f"posts/{post_id}/validate/", workspace=workspace)
    return {"ok": not problems, "problems_by_target": problems}


@mcp.tool(annotations=PUBLISH)
async def schedule_post(
    workspace: str,
    post_id: int,
    mode: Literal["now", "exact", "queue", "best_time"],
    at: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    comment: str = "",
) -> dict:
    """Schedule (or publish now) a post on all its channels.

    exact needs `at` (ISO 8601 with offset). best_time picks each channel's best free hour within the
    optional window. queue uses the next free queue slot. If the post needs team approval it is submitted
    for review instead, and `comment` is passed to the reviewer.
    """
    payload = {"mode": mode, "at": at, "window_start": window_start, "window_end": window_end, "comment": comment}
    data = await api(
        "POST",
        f"posts/{post_id}/schedule/",
        workspace=workspace,
        json={k: v for k, v in payload.items() if v not in (None, "")},
    )
    result = _post_summary(data)
    if data.get("_note"):
        result["note"] = data["_note"]
    return result


@mcp.tool(annotations=WRITE)
async def unschedule_post(workspace: str, post_id: int) -> dict:
    """Move a scheduled post back to draft on every channel that has not published yet."""
    return _post_summary(await api("POST", f"posts/{post_id}/unschedule/", workspace=workspace))


@mcp.tool(annotations=PUBLISH)
async def review_post(
    workspace: str, post_id: int, decision: Literal["approve", "request_changes"], comment: str = ""
) -> dict:
    """Approve a post waiting for your review (it is then scheduled as requested) or request changes."""
    path = "approve" if decision == "approve" else "request-changes"
    data = await api("POST", f"posts/{post_id}/{path}/", workspace=workspace, json={"comment": comment})
    result = _post_summary(data)
    if data.get("approval_outcome"):
        result["outcome"] = data["approval_outcome"]
    return result


# --- planning & insights --------------------------------------------------------------


@mcp.tool(annotations=READ)
async def get_calendar(workspace: str, start: str, end: str) -> list[dict]:
    """Everything scheduled or published between start and end (ISO 8601), one row per channel."""
    rows = await api("GET", "calendar/", workspace=workspace, params={"start": start, "end": end})
    return [
        {
            k: r.get(k)
            for k in (
                "post_id",
                "post_title",
                "excerpt",
                "channel_name",
                "provider",
                "status",
                "scheduled_at",
                "published_at",
                "external_url",
            )
        }
        for r in rows
    ]


@mcp.tool(annotations=READ)
async def get_best_times(workspace: str, channel_ids: list[int] | None = None) -> dict:
    """Best weekday/hour slots per channel, learned from past engagement (or platform defaults)."""
    params = {"channels": ",".join(map(str, channel_ids))} if channel_ids else {}
    data = await api("GET", "insights/best-times/", workspace=workspace, params=params)
    return {
        "timezone": data["timezone"],
        "channels": [
            {
                "channel_id": c["channel"],
                "name": c["name"],
                "source": c["source"],
                "sample_size": c["sample_size"],
                "top_slots": c["top"],
            }
            for c in data["channels"]
        ],
    }


@mcp.tool(annotations=READ)
async def suggest_times(
    workspace: str,
    channel_ids: list[int],
    body: str = "",
    post_id: int | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict:
    """Suggest a publishing time per channel for some content, with a short rationale."""
    payload = {
        "channels": channel_ids,
        "body": body,
        "post": post_id,
        "window_start": window_start,
        "window_end": window_end,
    }
    return await api(
        "POST", "insights/suggest/", workspace=workspace, json={k: v for k, v in payload.items() if v not in (None, "")}
    )


@mcp.tool(annotations=READ)
async def get_analytics(workspace: str, date_from: str | None = None, date_to: str | None = None) -> dict:
    """Totals, per-channel performance and top posts for a date range (YYYY-MM-DD; default last 30 days)."""
    params = {k: v for k, v in (("from", date_from), ("to", date_to)) if v}
    data = await api("GET", "analytics/overview/", workspace=workspace, params=params)
    return {k: data[k] for k in ("from", "to", "totals", "scheduled", "failed_last_7d", "channels", "top_posts")}


app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    # nginx in front validates the Host header; the internal hostname differs from the public one.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

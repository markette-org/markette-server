"""API documentation: the guide shown above the reference, tags, and the X-Workspace header hook.

Kept free of drf-spectacular imports so settings.py can import it."""

# Path prefixes of tenant-scoped endpoints, which need the X-Workspace header.
WORKSPACE_SCOPED = (
    "/api/channels/",
    "/api/posts/",
    "/api/targets/",
    "/api/calendar/",
    "/api/media/",
    "/api/campaigns/",
    "/api/slots/",
    "/api/members/",
    "/api/invitations/",
    "/api/teams/",
    "/api/analytics/",
    "/api/insights/",
)
WORKSPACE_HEADER = {
    "name": "X-Workspace",
    "in": "header",
    "required": True,
    "description": "Slug (or id) of the workspace to act in. List yours with `GET /api/workspaces/`.",
    "schema": {"type": "string", "example": "acme"},
}


def add_workspace_header(result, generator, request, public):
    for path, operations in result.get("paths", {}).items():
        if not path.startswith(WORKSPACE_SCOPED):
            continue
        for op in operations.values():
            if not isinstance(op, dict):
                continue
            params = op.setdefault("parameters", [])
            if not any(p.get("name") == "X-Workspace" for p in params):
                params.insert(0, dict(WORKSPACE_HEADER))
    return result


DESCRIPTION = """
The Markette REST API is what the Markette web app itself uses. With it you can publish, schedule and
measure posts across social networks and websites from your own scripts and tools.

## Authentication

Create a **personal API token** in *Settings → API & MCP* and send it with every request:

```
Authorization: Bearer mkt_xxxxxxxxxxxxxxxxxxxxxxxx
```

A token acts as you. It has your roles in every workspace, team channel access and approval rules
included. Revoke it at any time from the same page. Short-lived JWTs (`POST /api/auth/token/`) are also
accepted; they're what the web app uses.

## Workspaces

Most endpoints work inside one workspace. Pass its slug in the **`X-Workspace`** header:

```
X-Workspace: acme
```

`GET /api/workspaces/` lists the workspaces you can use, with your role and the workspace time zone.

## Quick start

```bash
TOKEN=mkt_…
API=https://your-markette-host/api

# 1. Your workspaces and channels
curl -H "Authorization: Bearer $TOKEN" $API/workspaces/
curl -H "Authorization: Bearer $TOKEN" -H "X-Workspace: acme" $API/channels/

# 2. Create a draft for two channels (per-channel text is optional)
curl -X POST -H "Authorization: Bearer $TOKEN" -H "X-Workspace: acme" -H "Content-Type: application/json" \\
  -d '{"body": "We are live!", "targets": [{"channel": 12}, {"channel": 15, "body": "Short version for X"}]}' \\
  $API/posts/

# 3. Schedule it: now, at an exact time, in the next queue slot, or at each channel's best time
curl -X POST -H "Authorization: Bearer $TOKEN" -H "X-Workspace: acme" -H "Content-Type: application/json" \\
  -d '{"mode": "best_time", "window_start": "2026-10-12T08:00:00+02:00", "window_end": "2026-10-14T20:00:00+02:00"}' \\
  $API/posts/42/schedule/
```

A schedule call returns **202** instead of 200 when a team requires approval. The post is then waiting
for a reviewer (`approval_status: "pending"`).

## Conventions

* **Lists** are paginated with `?limit=` and `?offset=` and return `{count, next, previous, results}`. The
  calendar and queue slots return plain arrays.
* **Times** are ISO 8601 with an offset. Stored and returned in UTC; the workspace time zone is used for
  queues, best times and analytics days.
* **Errors** use standard HTTP codes. The body is `{"detail": "…"}` or `{"field": ["…"]}`.
  * 400: validation.
  * 401: missing or invalid credentials.
  * 403: role, archived workspace or suspended organization.
  * 404: not found, or not in your workspace.
  * 429: slow down.
* **Media**: upload with `POST /api/media/` (multipart field `file`), then reference the returned ids in
  `media_ids`.

## MCP

The same capabilities are available to AI assistants through the Model Context Protocol at `/mcp`, with the
same tokens. See *Settings → API & MCP* for client setup.
"""

TAGS = [
    {"name": "auth", "description": "Sign-in, your profile and personal API tokens."},
    {"name": "workspaces", "description": "Workspaces you can use."},
    {"name": "organizations", "description": "Organizations, their members, invitations, shared channels and SSO."},
    {"name": "channels", "description": "Connected social accounts, blogs and sites (shared at organization level)."},
    {"name": "posts", "description": "Drafts, per-channel versions, scheduling and team approval."},
    {"name": "targets", "description": "One post on one channel: reschedule, cancel, delete from the network."},
    {"name": "calendar", "description": "Everything scheduled or published in a period."},
    {"name": "media", "description": "Images and videos to attach to posts."},
    {"name": "members", "description": "People in the current workspace and their roles."},
    {"name": "invitations", "description": "Pending invitations to the current workspace."},
    {"name": "campaigns", "description": "Periods with a theme that group posts."},
    {"name": "slots", "description": "Weekly queue times (workspace, team or channel)."},
    {"name": "teams", "description": "Publishing teams: channel access and approval."},
    {"name": "analytics", "description": "Metrics per post, channel and period."},
    {"name": "insights", "description": "Best times to post and AI assistance."},
    {
        "name": "notifications",
        "description": "Where the workspace hears about publications and problems: email, a Telegram bot, "
        "signed webhooks (admins only).",
    },
    {"name": "billing", "description": "Plans and subscriptions (Stripe)."},
    {"name": "providers", "description": "The networks and sites Markette can publish to, with their limits."},
    {"name": "platform", "description": "Installation administration (superadmins only)."},
]

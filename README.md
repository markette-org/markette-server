# markette-server

The Markette backend: a multi-tenant REST API (Django + DRF) with a Celery scheduler and workers that
publish to 21 networks and sites, collect metrics, learn the best time to post, and an MCP server
for AI assistants.

Part of [Markette](https://github.com/markette-org): see
[markette-deploy](https://github.com/markette-org/markette-deploy) to run the whole system.

## Layout

| Path | What |
|---|---|
| `markette/` | Django settings, URLs, Celery app, API docs text |
| `apps/core` | users, personal API tokens, encryption helpers |
| `apps/tenants` | organizations, workspaces, roles, invitations, plan limits |
| `apps/teams` | publishing teams: channel access and approval rules |
| `apps/channels` | connected accounts and the **providers** (one module per network) |
| `apps/publishing` | posts, per-channel targets, media, scheduling, approval workflow |
| `apps/analytics` | metric snapshots and audience history |
| `apps/insights` | best-time engine and AI helpers (OpenRouter / OpenAI-compatible) |
| `apps/billing` | plans and Stripe subscriptions |
| `apps/sso` | per-organization OpenID Connect |
| `apps/platform` | superadmin console API and installation settings |
| `mcp_server/` | Model Context Protocol server (Streamable HTTP and stdio) |
| `docs/openapi.yaml` | generated OpenAPI 3 schema (also served at `/api/schema/`) |

## Development

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL and Redis.

```bash
uv sync
cp .env.example .env            # POSTGRES_*, REDIS_URL…
uv run python manage.py migrate
uv run python manage.py runserver
uv run celery -A markette worker -l INFO
uv run celery -A markette beat -l INFO
uv run uvicorn mcp_server.server:app --port 8001   # optional: MCP server
```

Quality gates (also run in CI):

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest                    # needs PostgreSQL
uv run python manage.py spectacular --validate --file docs/openapi.yaml
```

The API reference is served at `/api/docs/` (Swagger) and `/api/redoc/`.

### Adding a network

Write a provider in `apps/channels/providers/` implementing `BaseProvider` (connect, validate,
publish, and optionally metrics, audience, delete and first comment), register it in
`providers/__init__.py`, add tests with simulated HTTP (`respx`), then the UI in
[markette-web](https://github.com/markette-org/markette-web). Check the platform's current API terms
first; the module docstrings record the rules each provider follows.

## License

[AGPL-3.0-only](LICENSE).

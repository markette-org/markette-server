"""drf-spectacular description of the personal API token authentication."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ApiTokenScheme(OpenApiAuthenticationExtension):
    target_class = "apps.core.authentication.ApiTokenAuthentication"
    name = "apiToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "mkt_…",
            "description": "Personal API token created in Settings → API & MCP. "
            "Send it as `Authorization: Bearer mkt_…`.",
        }

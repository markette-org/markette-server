import yaml
from rest_framework.test import APIClient


def test_reference_is_public_and_documents_auth_and_workspace(db):
    c = APIClient()
    schema = yaml.safe_load(c.get("/api/schema/").content)
    assert schema["info"]["title"] == "Markette API"
    assert "X-Workspace" in schema["info"]["description"]
    assert set(schema["components"]["securitySchemes"]) == {"apiToken", "jwtAuth"}
    post_create = schema["paths"]["/api/posts/"]["post"]
    assert any(p["name"] == "X-Workspace" for p in post_create["parameters"])
    assert "/api/billing/webhook/" not in schema["paths"]
    assert c.get("/api/docs/").status_code == 200 and c.get("/api/redoc/").status_code == 200

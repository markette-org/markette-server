from rest_framework.test import APIClient


def test_mobile_app_origins_allowed(db):
    for origin in ("capacitor://localhost", "https://localhost"):
        r = APIClient().options(
            "/api/auth/token/",
            HTTP_ORIGIN=origin,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,x-workspace,content-type",
        )
        assert r.headers.get("access-control-allow-origin") == origin
        assert "x-workspace" in r.headers.get("access-control-allow-headers", "")
    r = APIClient().options(
        "/api/auth/token/", HTTP_ORIGIN="https://evil.example", HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST"
    )
    assert "access-control-allow-origin" not in r.headers

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from rest_framework.test import APIClient

from apps.core.models import User
from apps.sso import service
from apps.sso.models import SSOConfig, SSODomain
from apps.tenants.models import Membership, OrgMembership, OrgRole

ISSUER = "https://idp.acme.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())), "kid": "k1", "alg": "RS256", "use": "sig"}


def id_token(nonce, email="ada@acme.com", aud="client-1", key=KEY, **extra):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": aud,
        "sub": "u-1",
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
        "email": email,
        "email_verified": True,
        "name": "Ada Lovelace",
        **extra,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def idp():
    service._discovery_cache.clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ISSUER}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                },
            )
        )
        mock.get(f"{ISSUER}/jwks").mock(return_value=httpx.Response(200, json={"keys": [JWK]}))
        yield mock


@pytest.fixture
def owner(user, workspace):
    OrgMembership.objects.create(organization=workspace.organization, user=user, role=OrgRole.OWNER)
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def sso(workspace, idp):
    cfg = SSOConfig.objects.create(
        organization=workspace.organization,
        enabled=True,
        issuer=ISSUER,
        client_id="client-1",
        secrets={"client_secret": "s3cret"},
        default_workspace=workspace,
    )
    SSODomain.objects.create(config=cfg, domain="acme.com", verified_at="2026-01-01T00:00:00Z")
    return cfg


def run_flow(idp, workspace, token_factory):
    anon = APIClient()
    disc = anon.get("/api/auth/sso/discover/", {"email": "ada@acme.com"}).data
    assert disc["sso"] is True
    start = anon.get(f"/api/auth/sso/start/{workspace.organization.slug}/")
    q = parse_qs(urlparse(start["Location"]).query)
    assert start["Location"].startswith(f"{ISSUER}/authorize") and q["code_challenge_method"] == ["S256"]
    idp.post(f"{ISSUER}/token").mock(
        return_value=httpx.Response(
            200, json={"id_token": token_factory(q["nonce"][0]), "access_token": "at", "token_type": "Bearer"}
        )
    )
    cb = anon.get("/api/auth/sso/callback/", {"code": "abc", "state": q["state"][0]})
    return anon, cb


def test_full_sign_in_provisions_user(idp, sso, workspace):
    anon, cb = run_flow(idp, workspace, lambda nonce: id_token(nonce))
    assert "/sso/complete?ticket=" in cb["Location"]
    ticket = parse_qs(urlparse(cb["Location"]).query)["ticket"][0]
    tokens = anon.post("/api/auth/sso/exchange/", {"ticket": ticket}, format="json").data
    assert tokens["user"]["email"] == "ada@acme.com" and tokens["access"]
    user = User.objects.get(email="ada@acme.com")
    assert not user.has_usable_password() and user.name == "Ada Lovelace"
    assert OrgMembership.objects.get(user=user).role == OrgRole.MEMBER
    assert Membership.objects.get(user=user, workspace=workspace).role == "editor"
    # Tickets are single use.
    assert anon.post("/api/auth/sso/exchange/", {"ticket": ticket}, format="json").status_code == 400
    token_req = next(c.request for c in idp.calls if c.request.url.path == "/token")
    assert token_req.headers["authorization"].startswith("Basic ") and b"code_verifier=" in token_req.content


@pytest.mark.parametrize(
    "factory,error",
    [
        (lambda n: id_token("wrong-nonce"), "nonce"),
        (lambda n: id_token(n, aud="someone-else"), "not valid"),
        (lambda n: id_token(n, key=rsa.generate_private_key(public_exponent=65537, key_size=2048)), "not valid"),
        (lambda n: id_token(n, email="eve@evil.com"), "not a verified domain"),
    ],
)
def test_bad_tokens_are_refused(idp, sso, workspace, factory, error):
    _, cb = run_flow(idp, workspace, factory)
    assert "/login?sso_error=" in cb["Location"] and error.split()[0] in cb["Location"].replace("+", " ")
    assert not User.objects.filter(email="ada@acme.com").exists()


def test_without_auto_provision_only_members_get_in(idp, sso, workspace):
    sso.auto_provision = False
    sso.save()
    _, cb = run_flow(idp, workspace, lambda n: id_token(n))
    assert "not+a+member" in cb["Location"]


def test_enforced_sso_blocks_password_login(sso, workspace):
    User.objects.create_user("bob@acme.com", "pw-pw-pw-pw-1")
    anon = APIClient()
    assert anon.post("/api/auth/token/", {"email": "bob@acme.com", "password": "pw-pw-pw-pw-1"}).status_code == 200
    sso.enforce = True
    sso.save()
    r = anon.post("/api/auth/token/", {"email": "bob@acme.com", "password": "pw-pw-pw-pw-1"})
    assert r.status_code == 403 and "single sign-on" in str(r.data)
    User.objects.create_superuser("root@acme.com", "pw-pw-pw-pw-1")
    assert anon.post("/api/auth/token/", {"email": "root@acme.com", "password": "pw-pw-pw-pw-1"}).status_code == 200


def test_owner_configures_and_verifies_domain(owner, workspace, idp, monkeypatch):
    org = workspace.organization
    r = owner.put(
        f"/api/organizations/{org.id}/sso/",
        {"issuer": ISSUER, "client_id": "client-1", "client_secret": "s", "enabled": True},
        format="json",
    )
    assert r.status_code == 200 and r.data["client_secret_set"] is True and "s" != r.data.get("client_secret")
    assert (
        owner.post(f"/api/organizations/{org.id}/sso/domains/", {"domain": "gmail.com"}, format="json").status_code
        == 400
    )
    r = owner.post(f"/api/organizations/{org.id}/sso/domains/", {"domain": "@Acme.com"}, format="json")
    dom = r.data["domains"][0]
    assert dom["domain"] == "acme.com" and dom["txt_name"] == "_markette-verification.acme.com"
    assert (
        owner.put(
            f"/api/organizations/{org.id}/sso/",
            {"issuer": ISSUER, "client_id": "client-1", "enabled": True, "enforce": True},
            format="json",
        ).status_code
        == 400
    )

    class Rec:
        def __init__(self, v):
            self.strings = [v.encode()]

    monkeypatch.setattr(
        service.dns.resolver, "resolve", lambda name, rtype, lifetime: [Rec("other"), Rec(dom["txt_value"])]
    )
    r = owner.post(f"/api/organizations/{org.id}/sso/domains/{dom['id']}/verify/")
    assert r.data["domains"][0]["verified"] is True
    assert APIClient().get("/api/auth/sso/discover/", {"email": "x@acme.com"}).data["organization"] == org.name


def test_only_owners_configure(workspace):
    admin = User.objects.create_user("adm@example.com", "pw-pw-pw-pw-1")
    OrgMembership.objects.create(organization=workspace.organization, user=admin, role=OrgRole.ADMIN)
    c = APIClient()
    c.force_authenticate(admin)
    r = c.get(f"/api/organizations/{workspace.organization_id}/sso/")
    assert r.status_code == 200 and r.data["can_edit"] is False
    assert (
        c.put(f"/api/organizations/{workspace.organization_id}/sso/", {"enabled": False}, format="json").status_code
        == 403
    )


def test_disable_without_provider_details(owner, workspace, sso):
    r = owner.put(f"/api/organizations/{workspace.organization_id}/sso/", {"enabled": False}, format="json")
    assert r.status_code == 200 and r.data["enabled"] is False and r.data["issuer"] == ISSUER


def test_domain_cannot_be_claimed_twice(owner, workspace, sso, db):
    from apps.tenants.models import Organization

    other = Organization.objects.create(name="Rival")
    SSOConfig.objects.create(organization=other, issuer=ISSUER, client_id="x")
    rival_owner = User.objects.create_user("o@rival.com", "pw-pw-pw-pw-1")
    OrgMembership.objects.create(organization=other, user=rival_owner, role=OrgRole.OWNER)
    c = APIClient()
    c.force_authenticate(rival_owner)
    r = c.post(f"/api/organizations/{other.id}/sso/domains/", {"domain": "acme.com"}, format="json")
    assert r.status_code == 400 and "already claimed" in str(r.data)

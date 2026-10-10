"""OpenID Connect authorization-code flow (with PKCE) against an organization's identity provider."""

import logging
import time
from urllib.parse import urlencode

import dns.exception
import dns.resolver
import httpx
import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.channels.providers.base import pkce_pair
from apps.tenants.models import Membership, OrgMembership

from .models import SSOConfig, SSODomain, SSOLoginState, SSOTicket

log = logging.getLogger(__name__)
User = get_user_model()
SIGNING_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
# Domains anyone can get a mailbox on can never be claimed for SSO.
PUBLIC_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "hotmail.it",
    "live.com",
    "live.it",
    "msn.com",
    "yahoo.com",
    "yahoo.it",
    "ymail.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "pm.me",
    "gmx.com",
    "gmx.net",
    "gmx.de",
    "mail.com",
    "yandex.com",
    "yandex.ru",
    "zoho.com",
    "libero.it",
    "virgilio.it",
    "tiscali.it",
    "alice.it",
    "tin.it",
    "email.it",
    "fastmail.com",
    "hey.com",
    "tutanota.com",
    "qq.com",
    "163.com",
    "web.de",
    "orange.fr",
    "free.fr",
    "laposte.net",
}
_discovery_cache: dict[str, tuple[float, dict]] = {}


class SSOError(Exception):
    pass


def redirect_uri():
    return f"{settings.PUBLIC_API_URL}/api/auth/sso/callback/"


def normalize_domain(value):
    domain = (value or "").strip().lower().lstrip("@").rstrip(".")
    if "://" in domain:
        domain = domain.split("://", 1)[1].split("/", 1)[0]
    if not domain or "." not in domain or " " in domain:
        raise ValidationError({"domain": "Enter a domain such as acme.com."})
    if domain in PUBLIC_DOMAINS:
        raise ValidationError({"domain": "Public email domains cannot be used for single sign-on."})
    return domain


def domain_of(email):
    return (email or "").rsplit("@", 1)[-1].strip().lower()


def config_for_email(email):
    """The enabled SSO configuration that owns this email's domain, if any."""
    d = SSODomain.objects.filter(domain=domain_of(email), verified_at__isnull=False, config__enabled=True)
    row = d.select_related("config__organization").first()
    return row.config if row and row.config.organization.is_active else None


def password_login_blocked(user):
    if user.is_superuser:
        return False  # break-glass access for platform operators
    cfg = config_for_email(user.email)
    return bool(cfg and cfg.enforce)


# --- discovery & domain verification ----------------------------------------------


def discovery(issuer):
    issuer = issuer.rstrip("/")
    cached = _discovery_cache.get(issuer)
    if cached and time.monotonic() - cached[0] < 3600:
        return cached[1]
    try:
        resp = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        doc = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SSOError(f"Could not read the identity provider's configuration at {issuer}: {exc}") from exc
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(key):
            raise SSOError(f"The identity provider's configuration has no {key}.")
    _discovery_cache[issuer] = (time.monotonic(), doc)
    return doc


def verify_domain(domain_row):
    try:
        answers = dns.resolver.resolve(domain_row.txt_name, "TXT", lifetime=8.0)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        raise SSOError(f"No TXT record found at {domain_row.txt_name}. DNS changes can take a while to appear.")
    except dns.exception.DNSException as exc:
        raise SSOError(f"DNS lookup failed: {exc.__class__.__name__}.") from exc
    values = {b"".join(r.strings).decode(errors="ignore").strip() for r in answers}
    if domain_row.txt_value not in values:
        raise SSOError(f"The TXT record at {domain_row.txt_name} does not contain {domain_row.txt_value}.")
    domain_row.verified_at = timezone.now()
    domain_row.save(update_fields=["verified_at", "updated_at"])


# --- flow ------------------------------------------------------------------------------


def authorization_url(config: SSOConfig, next_path=""):
    doc = discovery(config.issuer)
    verifier, challenge = pkce_pair()
    st = SSOLoginState.objects.create(config=config, code_verifier=verifier, next_path=_safe_next(next_path))
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri(),
        "scope": config.scopes or "openid email profile",
        "state": st.state,
        "nonce": st.nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{doc['authorization_endpoint']}?{urlencode(params)}"


def _safe_next(path):
    path = (path or "").strip()
    return path if path.startswith("/") and not path.startswith("//") else ""


def _exchange_code(config, doc, code, verifier):
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(), "code_verifier": verifier}
    secret = config.secrets.get("client_secret", "")
    methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    auth = None
    if secret and "client_secret_basic" in methods:
        auth = (config.client_id, secret)
    else:
        data["client_id"] = config.client_id
        if secret:
            data["client_secret"] = secret
    try:
        resp = httpx.post(
            doc["token_endpoint"], data=data, auth=auth, timeout=15.0, headers={"Accept": "application/json"}
        )
    except httpx.HTTPError as exc:
        raise SSOError(f"Could not reach the identity provider: {exc}") from exc
    if resp.status_code != 200:
        raise SSOError(f"The identity provider refused the sign-in ({resp.status_code}): {resp.text[:300]}")
    tokens = resp.json()
    if not tokens.get("id_token"):
        raise SSOError("The identity provider returned no ID token (is the 'openid' scope enabled?).")
    return tokens


def _validate_id_token(config, doc, id_token, nonce):
    try:
        header = jwt.get_unverified_header(id_token)
        jwks = httpx.get(doc["jwks_uri"], timeout=10.0).json()
    except (jwt.InvalidTokenError, httpx.HTTPError, ValueError) as exc:
        raise SSOError(f"Could not read the identity provider's signing keys: {exc}") from exc
    keys = [k for k in jwks.get("keys", []) if not header.get("kid") or k.get("kid") == header.get("kid")]
    if not keys:
        raise SSOError("The ID token is signed with an unknown key.")
    if header.get("alg") not in SIGNING_ALGS:
        raise SSOError(f"Unsupported ID token algorithm {header.get('alg')}.")
    try:
        claims = jwt.decode(
            id_token,
            key=jwt.PyJWK(keys[0]).key,
            algorithms=[header["alg"]],
            audience=config.client_id,
            issuer=doc["issuer"],
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=60,
        )
    except jwt.InvalidTokenError as exc:
        raise SSOError(f"The ID token is not valid: {exc}") from exc
    if claims.get("nonce") != nonce:
        raise SSOError("The ID token nonce does not match this sign-in.")
    return claims


def _email_from(claims):
    email = claims.get("email") or (
        claims.get("preferred_username") if "@" in str(claims.get("preferred_username")) else ""
    )
    if not email:
        raise SSOError("The identity provider did not share an email address (enable the 'email' scope).")
    if claims.get("email_verified") is False:
        raise SSOError("The identity provider says this email address is not verified.")
    return email.strip().lower()


@transaction.atomic
def _provision(config, email, claims):
    org = config.organization
    domain = domain_of(email)
    if not config.domains.filter(domain=domain, verified_at__isnull=False).exists():
        raise SSOError(f"{domain} is not a verified domain of {org.name}.")
    user = User.objects.filter(email__iexact=email).first()
    if user is not None and not user.is_active:
        raise SSOError("This account is deactivated.")
    member = OrgMembership.objects.filter(organization=org, user=user).exists() if user else False
    if not member and not config.auto_provision:
        raise SSOError(f"You are not a member of {org.name}. Ask an administrator to invite you.")
    if user is None:
        from apps.tenants.limits import check_member_limit

        check_member_limit(org, email=email)
        name = claims.get("name") or " ".join(filter(None, [claims.get("given_name"), claims.get("family_name")]))
        user = User.objects.create_user(email, None, name=name[:150])  # no usable password: SSO only
    if not member:
        OrgMembership.objects.create(organization=org, user=user, role=config.default_org_role)
        ws = config.default_workspace
        if ws and ws.organization_id == org.id:
            Membership.objects.get_or_create(workspace=ws, user=user, defaults={"role": config.default_workspace_role})
    return user


def complete(params):
    """Handle the identity provider's redirect; returns a one-time ticket."""
    if params.get("error"):
        raise SSOError(params.get("error_description") or params["error"])
    st = SSOLoginState.objects.select_related("config__organization").filter(pk=params.get("state", "")).first()
    if st is None or st.expires_at < timezone.now():
        raise SSOError("This sign-in link has expired. Start again.")
    st.delete()  # single use
    config = st.config
    if not config.enabled or not config.organization.is_active:
        raise SSOError("Single sign-on is disabled for this organization.")
    doc = discovery(config.issuer)
    tokens = _exchange_code(config, doc, params.get("code", ""), st.code_verifier)
    claims = _validate_id_token(config, doc, tokens["id_token"], st.nonce)
    user = _provision(config, _email_from(claims), claims)
    log.info("SSO sign-in for %s via %s", user.email, config.organization.slug)
    return SSOTicket.objects.create(user=user, next_path=st.next_path)


def redeem(code):
    ticket = SSOTicket.objects.select_related("user").filter(pk=code).first()
    if ticket is None or ticket.expires_at < timezone.now():
        raise ValidationError({"detail": "This sign-in has expired. Start again."})
    ticket.delete()
    return ticket.user, ticket.next_path

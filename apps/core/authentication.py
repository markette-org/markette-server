from django.utils import timezone
from rest_framework import authentication, exceptions

from .models import ApiToken


class ApiTokenAuthentication(authentication.BaseAuthentication):
    """``Authorization: Bearer mkt_…`` personal access tokens (JWTs fall through to SimpleJWT)."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode(errors="ignore")
        parts = header.split()
        if len(parts) != 2 or parts[0] != self.keyword or not parts[1].startswith(ApiToken.PREFIX):
            return None
        token = ApiToken.objects.select_related("user").filter(token_hash=ApiToken.hash(parts[1])).first()
        now = timezone.now()
        if token is None or (token.expires_at and token.expires_at < now) or not token.user.is_active:
            raise exceptions.AuthenticationFailed("Invalid or expired API token.")
        if not token.last_used_at or (now - token.last_used_at).total_seconds() > 60:
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return token.user, token

    def authenticate_header(self, request):
        return self.keyword

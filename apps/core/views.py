from datetime import timedelta

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.docs import doc

from .models import ApiToken


def _row(t):
    return {
        "id": t.id,
        "name": t.name,
        "hint": t.hint,
        "created_at": t.created_at,
        "last_used_at": t.last_used_at,
        "expires_at": t.expires_at,
    }


@doc(
    "auth",
    "List or create personal API tokens",
    description='POST {"name": "…", "expires_in_days": 90} returns the token **once** in `token`.',
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def tokens(request):
    if request.method == "GET":
        return Response([_row(t) for t in request.user.api_tokens.all()])
    name = (request.data.get("name") or "").strip()[:80]
    if not name:
        raise ValidationError({"name": "Give the token a name, e.g. 'Claude Desktop'."})
    days = request.data.get("expires_in_days")
    expires = None
    if days not in (None, "", 0, "0"):
        try:
            expires = timezone.now() + timedelta(days=int(days))
        except (TypeError, ValueError):
            raise ValidationError({"expires_in_days": "A number of days, or empty for no expiry."})
    token, raw = ApiToken.issue(request.user, name, expires)
    # The raw token is returned once and never stored.
    return Response({**_row(token), "token": raw}, status=status.HTTP_201_CREATED)


@doc("auth", "Revoke a personal API token", responses={204: None})
@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def revoke_token(request, token_id):
    get_object_or_404(ApiToken, pk=token_id, user=request.user).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)

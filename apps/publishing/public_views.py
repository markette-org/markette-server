"""Pages opened without an account: client review links."""

from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from apps.core.docs import doc

from . import client_review
from .models import ReviewLink


class ReviewThrottle(AnonRateThrottle):
    rate = "60/hour"


def _link(token):
    link = get_object_or_404(ReviewLink.objects.select_related("post__workspace"), token=token)
    if not link.active or not link.post.workspace.is_live:
        raise NotFound("This review link has expired or was withdrawn.")
    return link


@doc("posts", "Review a post through a review link (no account)")
@api_view(["GET", "POST"])
@permission_classes([AllowAny])
@throttle_classes([ReviewThrottle])
def review(request, token):
    link = _link(token)
    if request.method == "POST":
        client_review.decide(link, request.data.get("decision"), request.data.get("name"), request.data.get("comment"))
        link.post.refresh_from_db()
    return Response(client_review.preview(link))

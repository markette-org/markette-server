"""Shorthands for documenting function-based views and viewset actions in the OpenAPI schema."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema

OBJECT = OpenApiTypes.OBJECT


def doc(tag, summary, *, description="", request=None, responses=OBJECT, params=()):
    return extend_schema(
        tags=[tag],
        summary=summary,
        description=description,
        request=request,
        responses=responses,
        parameters=list(params),
    )


def query(name, description, type=str, required=False):
    return OpenApiParameter(name, type, OpenApiParameter.QUERY, required=required, description=description)


hidden = extend_schema(exclude=True)

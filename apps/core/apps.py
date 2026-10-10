from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self):
        from . import schema  # noqa: F401 - registers the OpenAPI auth extension

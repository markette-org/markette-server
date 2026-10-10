import pytest
from django.conf import settings as dj_settings
from rest_framework.test import APIClient

from apps.channels.models import Channel
from apps.channels.providers import REGISTRY, BaseProvider, Capabilities, Metrics, ProviderError, PublishResult
from apps.core.models import User
from apps.tenants.models import Membership, Organization, Role, Workspace


class FakeProvider(BaseProvider):
    """In-memory provider: records publications, can be told to fail."""

    key = "fake"
    label = "Fake"
    capabilities = Capabilities(max_chars=280, metric_names=("impressions", "likes"))
    published: list = []
    fail_with: ProviderError | None = None
    metrics: dict = {}

    def publish(self, content):
        if FakeProvider.fail_with:
            raise FakeProvider.fail_with
        ext = f"fake-{len(FakeProvider.published) + 1}"
        FakeProvider.published.append((self.channel.pk, content))
        return PublishResult(external_id=ext, url=f"https://fake.example/{ext}")

    def fetch_metrics(self, external_ids):
        return {i: FakeProvider.metrics.get(i, Metrics(impressions=100, likes=7)) for i in external_ids}


@pytest.fixture(autouse=True)
def _settings(settings, tmp_path):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.HUGO_WORKDIR = tmp_path / "hugo"
    settings.AI_ENABLED = False
    from markette.celery import app

    app.conf.task_always_eager = True
    yield
    app.conf.task_always_eager = dj_settings.CELERY_TASK_ALWAYS_EAGER


@pytest.fixture
def fake_provider():
    REGISTRY["fake"] = FakeProvider
    FakeProvider.published = []
    FakeProvider.fail_with = None
    FakeProvider.metrics = {}
    yield FakeProvider
    REGISTRY.pop("fake", None)


@pytest.fixture
def user(db):
    return User.objects.create_user("ada@example.com", "s3cret-pass!", name="Ada")


@pytest.fixture
def workspace(user):
    ws = Workspace.objects.create(
        organization=Organization.objects.create(name="Acme"), name="Acme", timezone="Europe/Rome"
    )
    Membership.objects.create(workspace=ws, user=user, role=Role.OWNER)
    return ws


@pytest.fixture
def api(user, workspace):
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_WORKSPACE=workspace.slug)
    return client


@pytest.fixture
def channel(workspace, fake_provider):
    return Channel.create_for(
        workspace=workspace, provider="fake", external_id="1", name="Fake One", credentials={"token": "t"}
    )

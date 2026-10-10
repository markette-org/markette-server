import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Channel, ChannelStatus
from .providers import ProviderError

log = logging.getLogger(__name__)


@shared_task
def refresh_expiring_tokens():
    soon = timezone.now() + timedelta(days=7)
    for channel in Channel.objects.filter(status=ChannelStatus.ACTIVE, token_expires_at__lte=soon):
        try:
            channel.get_provider().refresh_credentials()
        except ProviderError as exc:
            log.warning("Token refresh failed for channel %s: %s", channel.pk, exc)
            if exc.auth:
                channel.mark_needs_reauth(str(exc))

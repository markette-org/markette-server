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


@shared_task
def test_upload_bot(channel_id):
    """Runs a channel's upload-bot recipe without uploading (uploader worker): is it signed in, is the form there?"""
    from django.conf import settings

    from .providers.uploader import HandoffRequired, run

    channel = Channel.objects.filter(pk=channel_id).first()
    if channel is None:
        return "gone"
    shot = settings.MEDIA_ROOT / "bot-tests" / f"channel-{channel.pk}.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = run(channel.get_provider(), dry_run=True, screenshot=str(shot))
        missing = [r["step"] for r in result["report"] if r.get("found") is False]
        outcome = {
            "status": "ok" if not missing else "incomplete",
            "message": "Signed in and the upload form was found."
            if not missing
            else f"Some parts of the form were not found: {missing}",
        }
    except HandoffRequired as exc:
        outcome = {"status": "failed", "message": str(exc)}
    outcome.update(
        at=timezone.now().isoformat(),
        screenshot=f"{settings.PUBLIC_API_URL}/media/bot-tests/channel-{channel.pk}.png" if shot.exists() else "",
    )
    channel.config = {**(channel.config or {}), "bot_test": outcome}
    channel.save(update_fields=["config", "updated_at"])
    return outcome["status"]

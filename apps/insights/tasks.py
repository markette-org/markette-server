from celery import shared_task
from django.utils import timezone

from apps.channels.models import Channel, ChannelStatus

from .engine import compute_grid
from .models import BestTimeProfile


@shared_task
def rebuild_all_best_times():
    for channel in Channel.objects.filter(status=ChannelStatus.ACTIVE).select_related("organization"):
        scores, n, counts = compute_grid(channel)
        BestTimeProfile.objects.update_or_create(
            channel=channel,
            defaults={"scores": scores, "counts": counts, "sample_size": n, "computed_at": timezone.now()},
        )

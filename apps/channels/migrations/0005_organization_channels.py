"""Move channels from workspaces to organizations.

Every channel gets its workspace's organization and keeps access from that
workspace. Accounts connected separately in several workspaces of the same
organization (same provider + external id) are merged into one shared channel;
their publications, slots, audience history and team assignments move to it.
"""

import django.db.models.deletion
from django.db import migrations, models


def forwards(apps, schema_editor):
    Channel = apps.get_model("channels", "Channel")
    ChannelAccess = apps.get_model("channels", "ChannelAccess")
    PostTarget = apps.get_model("publishing", "PostTarget")
    PostingSlot = apps.get_model("publishing", "PostingSlot")
    AudienceSnapshot = apps.get_model("analytics", "AudienceSnapshot")
    BestTimeProfile = apps.get_model("insights", "BestTimeProfile")
    Team = apps.get_model("teams", "Team")

    keepers = {}
    for ch in Channel.objects.select_related("workspace").order_by("created_at", "id"):
        org_id = ch.workspace.organization_id
        key = (org_id, ch.provider, ch.external_id)
        keeper = keepers.get(key)
        if keeper is None:
            ch.organization_id = org_id
            ch.save(update_fields=["organization"])
            ChannelAccess.objects.get_or_create(channel=ch, workspace_id=ch.workspace_id)
            keepers[key] = ch
            continue
        # Duplicate of an account already connected in another workspace: merge into the keeper.
        ChannelAccess.objects.get_or_create(channel=keeper, workspace_id=ch.workspace_id)
        PostTarget.objects.filter(channel=ch).update(channel=keeper)
        PostingSlot.objects.filter(channel=ch).update(channel=keeper)
        taken = set(AudienceSnapshot.objects.filter(channel=keeper).values_list("date", flat=True))
        AudienceSnapshot.objects.filter(channel=ch, date__in=taken).delete()
        AudienceSnapshot.objects.filter(channel=ch).update(channel=keeper)
        BestTimeProfile.objects.filter(channel=ch).delete()
        for team in Team.objects.filter(channels=ch):
            team.channels.add(keeper)
        ch.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("channels", "0004_add_telegram"),
        ("tenants", "0004_workspace_org_required"),
        ("publishing", "0003_postingslot_team_postingslot_uniq_slot_v2_and_more"),
        ("analytics", "0003_initial"),
        ("insights", "0002_ai_cost"),
        ("teams", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="channel",
            name="workspace",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="legacy_channels", to="tenants.workspace"
            ),
        ),
        migrations.AddField(
            model_name="channel",
            name="organization",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE, related_name="channels",
                to="tenants.organization",
            ),
        ),
        migrations.CreateModel(
            name="ChannelAccess",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "channel",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="access", to="channels.channel"
                    ),
                ),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="channel_access",
                        to="tenants.workspace",
                    ),
                ),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("channel", "workspace"), name="uniq_channel_access")]
            },
        ),
        migrations.AddField(
            model_name="channel",
            name="workspaces",
            field=models.ManyToManyField(
                blank=True, related_name="channels", through="channels.ChannelAccess", to="tenants.workspace"
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="channel", name="uniq_channel"),
        migrations.RemoveField(model_name="channel", name="workspace"),
        migrations.AlterField(
            model_name="channel",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="channels", to="tenants.organization"
            ),
        ),
        migrations.AddConstraint(
            model_name="channel",
            constraint=models.UniqueConstraint(
                fields=("organization", "provider", "external_id"), name="uniq_org_channel"
            ),
        ),
    ]

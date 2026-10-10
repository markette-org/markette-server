import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenants", "0003_backfill_organizations")]
    operations = [
        migrations.AlterField(
            model_name="workspace",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="workspaces", to="tenants.organization"
            ),
        ),
    ]

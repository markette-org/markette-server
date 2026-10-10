from django.db import migrations
from django.utils.text import slugify


def backfill(apps, schema_editor):
    Organization = apps.get_model("tenants", "Organization")
    OrgMembership = apps.get_model("tenants", "OrgMembership")
    Workspace = apps.get_model("tenants", "Workspace")
    Membership = apps.get_model("tenants", "Membership")
    for ws in Workspace.objects.filter(organization__isnull=True):
        base = slugify(ws.name)[:120] or "organization"
        slug, n = base, 1
        while Organization.objects.filter(slug=slug).exists():
            n += 1
            slug = f"{base}-{n}"
        org = Organization.objects.create(name=ws.name, slug=slug)
        ws.organization = org
        ws.save(update_fields=["organization"])
        for m in Membership.objects.filter(workspace=ws, role="owner"):
            OrgMembership.objects.get_or_create(organization=org, user_id=m.user_id, defaults={"role": "owner"})


class Migration(migrations.Migration):
    dependencies = [("tenants", "0002_organizations")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]

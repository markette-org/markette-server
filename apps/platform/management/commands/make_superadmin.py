from django.core.management.base import BaseCommand, CommandError

from apps.core.models import User


class Command(BaseCommand):
    help = "Grant (or with --revoke, remove) platform superadmin rights to an existing user."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--revoke", action="store_true")

    def handle(self, email, revoke=False, **opts):
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            raise CommandError(f"No user with email {email}.")
        user.is_superuser = user.is_staff = not revoke
        user.save(update_fields=["is_superuser", "is_staff"])
        self.stdout.write(self.style.SUCCESS(f"{user.email} is {'no longer ' if revoke else ''}a superadmin."))

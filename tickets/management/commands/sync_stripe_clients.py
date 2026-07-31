"""Reconcile every TimeTrax client with Stripe (the manual re-sync).

    python manage.py sync_stripe_clients

TimeTrax is the source of truth → this pushes clients to Stripe, bonding new ones
and updating names/emails on existing ones (dedup-safe). Also runnable later by Hermes.
"""

from django.core.management.base import BaseCommand

from tickets import billing


class Command(BaseCommand):
    help = "Sync all TimeTrax clients to Stripe (create/link customers, push name/email)."

    def handle(self, *args, **options):
        try:
            counts, errors = billing.sync_all_clients()
        except billing.BillingError as e:
            self.stderr.write(self.style.ERROR(str(e)))
            return

        if counts:
            summary = ", ".join(f"{n} {action}" for action, n in counts.items())
            self.stdout.write(self.style.SUCCESS(f"Synced: {summary}"))
        else:
            self.stdout.write("No clients to sync.")

        for err in errors:
            self.stderr.write(self.style.WARNING(f"  ! {err}"))

"""Wipe one shop back to its menu.

    python manage.py reset_shop coop --dry-run
    python manage.py reset_shop coop --yes --firebase

Same code path as the admin button (apps/store/reset.py); this is the version
you can run when the admin is not reachable, or from a Dokploy shell.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.store import reset as reset_service
from apps.store.models import Store


class Command(BaseCommand):
    help = "Delete a shop's history (orders, sales, customers, points) and keep its menu."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Store slug, e.g. coop")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would be deleted and stop.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the typed confirmation (for scripts).",
        )
        parser.add_argument(
            "--firebase",
            action="store_true",
            help="Also delete the customers' Firebase accounts so they can sign up again.",
        )

    def handle(self, *args, **opts):
        try:
            store = Store.objects.unscoped().get(slug=opts["slug"])
        except Store.DoesNotExist as exc:
            raise CommandError(f"No store with slug {opts['slug']!r}") from exc

        counts = reset_service.preview(store)
        uids = reset_service.firebase_uids(store)
        width = max(len(label) for label, _ in counts)
        self.stdout.write(self.style.WARNING(f"\n{store.name} ({store.slug}) — would delete:"))
        for label, n in counts:
            self.stdout.write(f"  {label:<{width}}  {n}")
        self.stdout.write(f"  {'Firebase accounts':<{width}}  {len(uids)}")
        self.stdout.write("\nKept: " + "; ".join(reset_service.KEPT))

        if opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS("\nDry run — nothing was deleted."))
            return

        if not opts["yes"]:
            typed = input(f"\nType the slug {store.slug!r} to confirm: ").strip()
            if typed != store.slug:
                raise CommandError("Confirmation did not match. Nothing was deleted.")

        done = reset_service.wipe_database(store)
        self.stdout.write(self.style.SUCCESS(f"\nDeleted {sum(n for _, n in done)} rows."))

        if opts["firebase"]:
            n, note = reset_service.delete_firebase_users(uids)
            self.stdout.write((self.style.SUCCESS if n else self.style.WARNING)(note))

"""Drain the price-check scan counters from Redis into ScanDaily.

Every run — the nightly Dokploy schedule at 01:00, or a manual trigger — folds
whatever is currently in Redis (all days, including today) into the DB and
resets the Redis counters to zero, atomically. Safe to run any time.
"""
from django.core.management.base import BaseCommand

from apps.store import scan_tracking


class Command(BaseCommand):
    help = (
        "Drain the price-check scan counters from Redis into the ScanDaily table "
        "and reset them to zero (atomic capture + clear). Runs nightly at 01:00; "
        "safe to trigger manually any time."
    )

    def handle(self, *args, **options):
        stats = scan_tracking.flush(write=self.stdout.write)
        self.stdout.write(
            self.style.SUCCESS(
                f"done — {stats['rows']} rows across {stats['days']} store-days"
            )
        )

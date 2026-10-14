"""Bring the sales rung on Extra P since the last import into this store.

    python manage.py sync_extrap coop               # asks for the password
    python manage.py sync_extrap coop --dry-run     # count only, change nothing

Signs in to Extra P (reads only), finds every sale not yet loaded — new sales,
and returns made since on old ones — and loads them exactly like the first
import: real time of day, cash/card, every line; no stock moved, no points
given. New drinks added on Extra P come along; drinks already here are left as
the shop has them. Safe to run as often as you like.

Credentials: --username / EXTRAP_USERNAME, and EXTRAP_PASSWORD or a prompt.
"""
import getpass
import os

from django.core.management.base import BaseCommand, CommandError

from apps.store import extrap, models
from apps.store.extrap_sync import DEFAULT_URL, ExtraP, SyncError, build_dump, _num


class Command(BaseCommand):
    help = "Import the Extra P sales that are not in this store yet."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Store slug, e.g. coop")
        parser.add_argument("--username", default=os.getenv("EXTRAP_USERNAME", ""))
        parser.add_argument("--url", default=os.getenv("EXTRAP_URL", DEFAULT_URL))
        parser.add_argument("--dry-run", action="store_true", help="Count what would come in; change nothing.")

    def handle(self, *args, **o):
        try:
            store = models.Store.objects.get(slug=o["slug"])
        except models.Store.DoesNotExist as exc:
            raise CommandError(f"No store with slug {o['slug']!r}") from exc

        username = o["username"] or input("Extra P username: ").strip()
        password = os.getenv("EXTRAP_PASSWORD") or getpass.getpass("Extra P password: ")

        have = {
            n.split()[0].removeprefix(extrap.NOTE)
            for n in models.Sale.objects.for_pharmacy(store.pk)
            .filter(note__startswith=extrap.NOTE).values_list("note", flat=True)
        }
        last = max((int(x) for x in have if x.isdigit()), default=0)

        client = ExtraP(o["url"])
        try:
            self.stdout.write("Signing in to Extra P…")
            client.login(username, password)
            self.stdout.write("Reading the sales list…")
            rows = client.all_sales()
            want = [
                r for r in rows
                if str(int(r["id"])) not in have
                or (_num(r.get("returned_amount", 0)) > 0 and f"R{int(r['id'])}" not in have)
            ]
            self.stdout.write(
                f"Extra P has {len(rows)} sales (newest #{int(rows[-1]['id']) if rows else 0}); "
                f"already here up to #{last}. To bring in: {len(want)}."
            )
            if not want:
                self.stdout.write(self.style.SUCCESS("Nothing new — everything is already here."))
                return
            self.stdout.write("Reading each new sale's lines and payment…")
            dump = build_dump(client, want)
        except SyncError as exc:
            raise CommandError(str(exc)) from exc

        report = extrap.load(store, dump, do_wipe=False, dry_run=o["dry_run"])
        label = "WOULD import (dry run)" if o["dry_run"] else "Imported"
        self.stdout.write(self.style.SUCCESS(
            f"{label}: {report['sales']} sales ({report['sales_skipped']} already here), "
            f"{report['lines']} lines, {report['products']} new drinks. "
            f"Cash ₪{report['cash']}, card ₪{report['card']}, returns ₪{report['returns']}. "
            f"From {report['first']} to {report['last']}."
        ))
        if report.get("lines_unmatched"):
            self.stdout.write(f"  {report['lines_unmatched']} lines did not match a drink (kept by name).")

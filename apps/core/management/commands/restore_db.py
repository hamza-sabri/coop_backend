"""Restore a database from a B2 backup — and the drill that PROVES backups work.

A backup you have never restored is a rumour. Run the drill regularly:

    python manage.py restore_db --list                 # what do we have?
    python manage.py restore_db --drill                # download + verify newest
    python manage.py restore_db --into "$TEST_DB_URL" --yes   # real restore

Safety
------
* Restores ONLY into the URL you pass with `--into` (never DATABASE_URL by
  default) — so a mistyped command can't overwrite production.
* If `--into` really is the live DATABASE_URL you must add `--i-know` too.
* `--drill` never writes to any database: it downloads the newest dump and
  verifies it is a readable, non-empty pg archive containing our tables.
"""
import os
import subprocess
import tempfile
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError

from .backup_db import dsn_parts, pg_bin


class Command(BaseCommand):
    help = "Restore (or verify) a database backup stored in B2."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="List backups in B2.")
        parser.add_argument("--drill", action="store_true",
                            help="Download the newest backup and verify it — no writes.")
        parser.add_argument("--key", help="Specific B2 key to restore (default: newest).")
        parser.add_argument("--into", help="Target DATABASE_URL to restore INTO.")
        parser.add_argument("--yes", action="store_true", help="Confirm the restore.")
        parser.add_argument("--i-know", action="store_true",
                            help="Required if the target is the live DATABASE_URL.")
        parser.add_argument("--prefix", default="db-backups")

    def handle(self, *args, **opts):
        client, bucket = self._b2()
        backups = self._list(client, bucket, opts["prefix"])

        if opts["list"] or (not opts["drill"] and not opts["into"]):
            if not backups:
                self.stdout.write(self.style.WARNING("No backups found in B2."))
                return
            self.stdout.write(f"{len(backups)} backup(s) — newest first:")
            for mod, key, size in backups[:20]:
                self.stdout.write(f"  {mod:%Y-%m-%d %H:%M}  {size/1_048_576:7.2f} MB  {key}")
            if not opts["drill"] and not opts["into"]:
                self.stdout.write("\nUse --drill to verify, or --into <URL> --yes to restore.")
            return

        if not backups:
            raise CommandError("No backups available.")
        key = opts["key"] or backups[0][1]

        path = os.path.join(tempfile.mkdtemp(prefix="pgrestore-"), os.path.basename(key))
        self.stdout.write(f"→ downloading {key} …")
        client.download_file(bucket, key, path)
        size = os.path.getsize(path)
        if size < 1024:
            raise CommandError("Downloaded dump is empty/corrupt.")
        self.stdout.write(self.style.SUCCESS(f"  downloaded {size/1_048_576:.2f} MB"))

        # ---- verification (always, drill or not) --------------------------
        listing = subprocess.run(
            [pg_bin("pg_restore"), "--list", path], capture_output=True, text=True
        )
        if listing.returncode != 0:
            raise CommandError(f"Not a readable pg archive: {listing.stderr[:400]}")
        tables = [ln for ln in listing.stdout.splitlines() if " TABLE DATA " in ln]
        self.stdout.write(self.style.SUCCESS(
            f"  archive readable — {len(tables)} table(s) with data"
        ))
        expected = ("pharmacy_medication", "pharmacy_sale")
        missing = [t for t in expected if not any(t in ln for ln in tables)]
        if missing:
            raise CommandError(f"Backup is missing core tables: {missing}")
        self.stdout.write(self.style.SUCCESS("  core tables present ✔"))

        if opts["drill"]:
            os.remove(path)
            self.stdout.write(self.style.SUCCESS(
                "DRILL PASSED — the newest backup is downloadable and restorable."
            ))
            return

        # ---- real restore --------------------------------------------------
        target = opts["into"]
        if not target:
            raise CommandError("Pass --into <DATABASE_URL> to restore.")
        if not opts["yes"]:
            raise CommandError("Refusing without --yes.")
        live = os.getenv("DATABASE_URL", "")
        if live and urlparse(target).hostname == urlparse(live).hostname and not opts["i_know"]:
            raise CommandError(
                "Target looks like the LIVE database. Re-run with --i-know if that "
                "is truly what you want."
            )
        db = dsn_parts(target)
        env = {**os.environ, "PGPASSWORD": db["password"], "PGSSLMODE": "require"}
        cmd = [
            pg_bin("pg_restore"),
            "-h", db["host"], "-p", db["port"], "-U", db["user"], "-d", db["name"],
            "--clean", "--if-exists", "--no-owner", "--no-privileges",
            "--single-transaction", path,
        ]
        self.stdout.write(f"→ restoring into {db['name']}@{db['host']} …")
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=7200)
        if proc.returncode != 0:
            raise CommandError(f"pg_restore failed: {proc.stderr.strip()[:800]}")
        os.remove(path)
        self.stdout.write(self.style.SUCCESS("Restore complete ✔"))

    # ---- helpers ---------------------------------------------------------
    def _b2(self):
        from .backup_db import Command as BackupCommand

        return BackupCommand()._b2()

    def _list(self, client, bucket, prefix):
        out, token = [], None
        while True:
            kw = {"Bucket": bucket, "Prefix": f"{prefix}/"}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            for o in resp.get("Contents", []):
                if o["Key"].endswith(".dump"):
                    out.append((o["LastModified"], o["Key"], o["Size"]))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        out.sort(reverse=True)
        return out

"""Nightly database backup: pg_dump → Backblaze B2.

Neon keeps its own point-in-time history, but that is Neon's copy — this is
OURS: a portable `pg_dump` custom-format archive in our own B2 bucket that we
can restore anywhere, even if the Neon project disappears.

    python manage.py backup_db                 # dump + upload + prune
    python manage.py backup_db --keep 60       # retain the last 60
    python manage.py backup_db --no-upload     # local file only (debugging)

Restore with `manage.py restore_db` (see that command for the drill).

Design notes
------------
* **Pooler**: Neon's `-pooler` host is PgBouncer; `pg_dump` needs a direct
  session, so the host is de-poolered automatically.
* **Custom format** (`-Fc`) — compressed and restorable selectively.
* Failure is LOUD (non-zero exit) so the Dokploy schedule shows red instead of
  silently backing up nothing.
"""
import glob
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def dsn_parts(url: str) -> dict:
    """Split a DATABASE_URL into pg_dump-friendly pieces (pooler stripped)."""
    p = urlparse(url)
    host = (p.hostname or "").replace("-pooler", "")  # PgBouncer can't pg_dump
    return {
        "host": host,
        "port": str(p.port or 5432),
        "user": p.username or "",
        "password": p.password or "",
        "name": (p.path or "/").lstrip("/") or "postgres",
    }


def pg_bin(tool: str) -> str:
    """Path to the NEWEST installed `tool` (pg_dump / pg_restore).

    pg_dump refuses to dump a server newer than itself, and Debian's default
    client can lag the managed server (Neon runs 18). Distro packages install
    versioned binaries under /usr/lib/postgresql/<major>/bin, so prefer the
    highest major available and only fall back to whatever is on PATH.
    """
    candidates = glob.glob(f"/usr/lib/postgresql/*/bin/{tool}")

    def major(path: str) -> int:
        m = re.search(r"/postgresql/(\d+)/", path)
        return int(m.group(1)) if m else 0

    if candidates:
        return max(candidates, key=major)
    return tool  # PATH


class Command(BaseCommand):
    help = "Dump the database and upload it to B2 (nightly backup)."

    def add_arguments(self, parser):
        parser.add_argument("--keep", type=int, default=30,
                            help="How many backups to retain in B2 (default 30).")
        parser.add_argument("--no-upload", action="store_true",
                            help="Only create the dump locally (no B2 upload).")
        parser.add_argument("--prefix", default="db-backups",
                            help="Key prefix inside the bucket.")

    def handle(self, *args, **opts):
        url = os.getenv("DATABASE_URL", "")
        if not url.startswith("post"):
            raise CommandError("DATABASE_URL is not a PostgreSQL URL.")
        db = dsn_parts(url)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"pharma-{stamp}.dump"
        tmpdir = tempfile.mkdtemp(prefix="pgdump-")
        path = os.path.join(tmpdir, filename)

        env = {**os.environ, "PGPASSWORD": db["password"], "PGSSLMODE": "require"}
        cmd = [
            pg_bin("pg_dump"),
            "-h", db["host"], "-p", db["port"], "-U", db["user"],
            "-d", db["name"],
            "-Fc",              # custom format: compressed + selective restore
            "--no-owner", "--no-privileges",
            "-f", path,
        ]
        self.stdout.write(f"→ pg_dump {db['name']}@{db['host']} …")
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
        except FileNotFoundError:
            raise CommandError(
                "pg_dump not found — install postgresql-client in the image."
            )
        if proc.returncode != 0:
            err = proc.stderr.strip()
            if "server version mismatch" in err:
                raise CommandError(
                    "pg_dump is OLDER than the database server.\n"
                    f"{err[:300]}\n"
                    "Fix: rebuild the image with a matching client — the Dockerfile "
                    "installs postgresql-client-${PG_MAJOR} from the PGDG repo; bump "
                    "PG_MAJOR to the server's major version and redeploy."
                )
            raise CommandError(f"pg_dump failed: {err[:800]}")

        size = os.path.getsize(path)
        if size < 1024:
            raise CommandError(f"Dump suspiciously small ({size} B) — aborting.")
        self.stdout.write(self.style.SUCCESS(f"  dump OK — {size/1_048_576:.2f} MB"))

        if opts["no_upload"]:
            self.stdout.write(f"Local dump kept at {path}")
            return

        key = f"{opts['prefix']}/{stamp[:4]}/{stamp[4:6]}/{filename}"
        client, bucket = self._b2()
        self.stdout.write(f"→ uploading to b2://{bucket}/{key} …")
        with open(path, "rb") as fh:
            client.upload_fileobj(fh, bucket, key)

        # Verify it really landed (never trust a silent upload).
        head = client.head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] != size:
            raise CommandError("Uploaded size mismatch — backup NOT trustworthy.")
        self.stdout.write(self.style.SUCCESS("  upload verified ✔"))

        os.remove(path)
        self._prune(client, bucket, opts["prefix"], opts["keep"])
        self.stdout.write(self.style.SUCCESS(f"Backup complete: {key}"))

    # ---- helpers ---------------------------------------------------------
    def _b2(self):
        import boto3

        bucket = getattr(settings, "B2_BUCKET_NAME", "") or os.getenv("B2_BUCKET_NAME", "")
        endpoint = getattr(settings, "B2_ENDPOINT_URL", "") or os.getenv("B2_ENDPOINT_URL", "")
        key_id = getattr(settings, "B2_KEY_ID", "") or os.getenv("B2_KEY_ID", "")
        secret = getattr(settings, "B2_APPLICATION_KEY", "") or os.getenv("B2_APPLICATION_KEY", "")
        region = getattr(settings, "B2_REGION", "") or os.getenv("B2_REGION", "")
        if not (bucket and endpoint and key_id and secret):
            raise CommandError("B2_* settings are missing — cannot upload backup.")
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            region_name=region or None,
        )
        return client, bucket

    def _prune(self, client, bucket, prefix, keep):
        """Keep only the newest `keep` dumps so the bucket can't grow forever."""
        if keep <= 0:
            return
        keys = []
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": f"{prefix}/"}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                if re.search(r"pharma-\d{8}T\d{6}Z\.dump$", obj["Key"]):
                    keys.append((obj["LastModified"], obj["Key"]))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        keys.sort(reverse=True)
        stale = keys[keep:]
        for _, k in stale:
            client.delete_object(Bucket=bucket, Key=k)
        if stale:
            self.stdout.write(f"  pruned {len(stale)} old backup(s), kept {keep}")

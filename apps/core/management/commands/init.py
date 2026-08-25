"""
Project setup wizard — generates your .env in one command.

Interactive (asks for each value, with defaults):

    python manage.py init

One-liner with flags (great for the README / CI / repeatable setups):

    python manage.py init \
        -d postgres://user:pass@host/db?sslmode=require \
        -s "$(python -c 'import secrets;print(secrets.token_urlsafe(50))')" \
        -b my-b2-bucket \
        --domain api.example.com --yes

Notes:
- SECRET_KEY is auto-generated when you don't pass one.
- Any value you omit falls back to a sensible default (or blank = feature off).
- Use --yes for non-interactive, --force to overwrite an existing .env,
  and --migrate to run migrations right after (needs a real DATABASE_URL).
"""
import re
import secrets
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# key = .env variable · flag = CLI dest · short = optional short flag
FIELDS = [
    {"key": "DATABASE_URL", "flag": "database", "short": "-d",
     "prompt": "Postgres/Neon DATABASE_URL", "default": "", "required": True},
    {"key": "SECRET_KEY", "flag": "secret", "short": "-s",
     "prompt": "SECRET_KEY (blank = auto-generate)", "default": "",
     "secret": True, "autogen": True},
    {"key": "DEBUG", "flag": "debug",
     "prompt": "Enable DEBUG? (development only)", "default": "False", "bool": True},
    {"key": "ALLOWED_HOSTS", "flag": "allowed_hosts",
     "prompt": "ALLOWED_HOSTS (comma-separated)", "default": "*"},
    {"key": "DOMAIN", "flag": "domain",
     "prompt": "Deploy DOMAIN (Dokploy/Traefik; blank to skip)", "default": ""},
    # --- Backblaze B2 (all blank = local file storage) ---
    {"key": "B2_BUCKET_NAME", "flag": "bucket", "short": "-b",
     "prompt": "Backblaze B2 bucket name", "default": ""},
    {"key": "B2_KEY_ID", "flag": "b2_key_id", "prompt": "B2 key ID", "default": ""},
    {"key": "B2_APPLICATION_KEY", "flag": "b2_app_key",
     "prompt": "B2 application key", "default": "", "secret": True},
    {"key": "B2_ENDPOINT_URL", "flag": "b2_endpoint",
     "prompt": "B2 S3 endpoint URL", "default": ""},
    {"key": "B2_REGION", "flag": "b2_region", "prompt": "B2 region", "default": ""},
    # --- Optional services (blank = disabled) ---
    {"key": "REDIS_URL", "flag": "redis", "short": "-r",
     "prompt": "Redis URL (blank = in-memory cache)", "default": ""},
    {"key": "SENTRY_DSN", "flag": "sentry",
     "prompt": "Sentry DSN (blank = disabled)", "default": ""},
]


class Command(BaseCommand):
    help = "Interactive/flag-driven wizard that writes your .env."

    def add_arguments(self, parser):
        for f in FIELDS:
            names = ([f["short"]] if f.get("short") else []) + [
                "--" + f["flag"].replace("_", "-")
            ]
            if f.get("bool"):
                parser.add_argument(*names, dest=f["flag"], action="store_true",
                                    default=None, help=f["prompt"])
            else:
                parser.add_argument(*names, dest=f["flag"], default=None,
                                    help=f["prompt"])
        # Local Postgres fallback — used ONLY when no DATABASE_URL / -d is given.
        parser.add_argument("--db-name", dest="db_name", default=None,
                            help="Local DB name when no -d given (default: project folder name).")
        parser.add_argument("--db-user", dest="db_user", default=None,
                            help="Local Postgres user (default: postgres).")
        parser.add_argument("--db-password", dest="db_password", default=None,
                            help="Local Postgres password (default: postgres).")
        parser.add_argument("--db-host", dest="db_host", default=None,
                            help="Local Postgres host (default: localhost).")
        parser.add_argument("--db-port", dest="db_port", default=None,
                            help="Local Postgres port (default: 5432).")
        parser.add_argument("--no-create-db", action="store_true",
                            help="Don't try to create the local database.")
        parser.add_argument("-y", "--yes", action="store_true",
                            help="Non-interactive: use flags + defaults, no prompts.")
        parser.add_argument("--force", action="store_true",
                            help="Overwrite an existing .env.")
        parser.add_argument("--migrate", action="store_true",
                            help="Run migrations after writing .env.")

    # -- prompting helpers ---------------------------------------------------
    def _ask(self, prompt, default):
        suffix = f" [{default}]" if default else ""
        try:
            raw = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            raw = ""
        return raw or default

    def _ask_bool(self, prompt, default=False):
        d = "Y/n" if default else "y/N"
        try:
            raw = input(f"  {prompt} [{d}]: ").strip().lower()
        except EOFError:
            raw = ""
        if not raw:
            return default
        return raw in ("y", "yes", "true", "1")

    def handle(self, *args, **opts):
        base = Path(settings.BASE_DIR)
        env_path = base / ".env"
        example = base / ".env.example"

        if env_path.exists() and not opts["force"]:
            raise CommandError(".env already exists. Re-run with --force to overwrite.")

        interactive = not opts["yes"]
        if interactive:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "Setup wizard — press Enter to accept the [default].\n"))

        values = {}
        for f in FIELDS:
            provided = opts.get(f["flag"])
            if f.get("bool"):
                if provided:
                    val = "True"
                elif interactive:
                    val = "True" if self._ask_bool(f["prompt"], False) else "False"
                else:
                    val = f["default"]
            else:
                if provided is not None:
                    val = provided
                elif interactive:
                    val = self._ask(f["prompt"], f["default"])
                else:
                    val = f["default"]
            if f.get("autogen") and not val:
                val = secrets.token_urlsafe(50)
            values[f["key"]] = val

        # No database given? Fall back to a LOCAL Postgres DB named after the
        # project folder — perfect for local dev. Production should pass -d/Neon.
        local_db = None
        if not values["DATABASE_URL"]:
            name = opts["db_name"] or self._slug(Path(settings.BASE_DIR).name)
            user = opts["db_user"] or "postgres"
            pw = opts["db_password"] or "postgres"
            host = opts["db_host"] or "localhost"
            port = opts["db_port"] or "5432"
            values["DATABASE_URL"] = f"postgres://{user}:{pw}@{host}:{port}/{name}"
            local_db = (name, user, pw, host, port)

        self._write_env(env_path, example, values)

        # Summary (secrets masked).
        self.stdout.write(self.style.SUCCESS(f"\n✓ Wrote {env_path.name}"))
        for f in FIELDS:
            v = values[f["key"]]
            if not v:
                continue
            shown = self._mask(v) if f.get("secret") else v
            self.stdout.write(f"  {f['key']} = {shown}")

        if local_db:
            name, user, pw, host, port = local_db
            self.stdout.write(self.style.WARNING(
                f"\nNo --database given → using a LOCAL Postgres DB '{name}' "
                f"({user}@{host}:{port})."))
            self.stdout.write(
                "  (For production, re-run with -d <your Neon/Postgres URL>.)")
            if not opts["no_create_db"]:
                self._create_database(name, user, pw, host, port)

        if opts["migrate"] and values["DATABASE_URL"]:
            self.stdout.write("\nRunning migrations...")
            subprocess.run([sys.executable, "manage.py", "migrate"], cwd=base, check=False)

        self.stdout.write(self.style.MIGRATE_HEADING("\nNext steps:"))
        if not opts["migrate"]:
            self.stdout.write("  python manage.py migrate")
        self.stdout.write("  python manage.py createsuperuser")
        self.stdout.write("  python manage.py runserver\n")

    # -- writing -------------------------------------------------------------
    def _write_env(self, env_path: Path, example: Path, values: dict):
        if example.exists():
            lines = example.read_text(encoding="utf-8").splitlines()
            seen = set()
            out = []
            for line in lines:
                m = re.match(r"^([A-Z0-9_]+)=", line)
                if m and m.group(1) in values:
                    key = m.group(1)
                    out.append(f"{key}={values[key]}")
                    seen.add(key)
                else:
                    out.append(line)
            for key, val in values.items():
                if key not in seen:
                    out.append(f"{key}={val}")
            env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        else:
            env_path.write_text(
                "\n".join(f"{k}={v}" for k, v in values.items()) + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _mask(v: str) -> str:
        if len(v) <= 8:
            return "•" * len(v)
        return f"{v[:4]}…{v[-4:]}"

    @staticmethod
    def _slug(name: str) -> str:
        s = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")
        if not s or s[0].isdigit():
            s = "app_" + s
        return s or "app"

    def _create_database(self, name, user, pw, host, port):
        """Best-effort: create the local Postgres database if it doesn't exist."""
        try:
            import psycopg2
            from psycopg2 import sql

            conn = psycopg2.connect(dbname="postgres", user=user, password=pw,
                                    host=host, port=port, connect_timeout=5)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                if cur.fetchone():
                    self.stdout.write(f"  Database '{name}' already exists — good.")
                else:
                    cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                    self.stdout.write(self.style.SUCCESS(f"  ✓ Created local database '{name}'."))
            conn.close()
        except Exception as e:
            self.stdout.write(self.style.WARNING(
                f"  Couldn't auto-create '{name}': {e}"))
            self.stdout.write(
                f"  Is local Postgres running? Create it yourself with:  createdb {name}\n"
                f"  ...or pass --db-user/--db-password, or use -d <full DATABASE_URL>.")

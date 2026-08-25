"""
Create a new, fully-wired local app under apps/.

    python manage.py newapp catalog

This creates apps/catalog/ with models/serializers/views/urls/admin (each
carrying the scaffold anchors), registers "apps.catalog" in INSTALLED_APPS,
and — thanks to the URL auto-discovery in config/urls.py — mounts it under
/api/v1/ automatically. Then:

    # edit apps/catalog/models.py, add your model(s)
    python manage.py setup_model catalog CatalogItem
"""
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core import scaffold

APPS_PY = '''from django.apps import AppConfig


class {cls}Config(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.{name}"
    verbose_name = "{verbose}"
'''

MODELS_PY = '''from django.db import models

from apps.core.models import TimeStampedModel

# Define your models here, then run:
#   python manage.py setup_model {name} <ModelName>
#
# Example:
# class Example(TimeStampedModel):
#     name = models.CharField(max_length=200)
#
#     def __str__(self):
#         return self.name
'''


class Command(BaseCommand):
    help = "Create a new local app under apps/ and register it in INSTALLED_APPS."

    def add_arguments(self, parser):
        parser.add_argument("name", help="App name, e.g. catalog (lowercase, no spaces)")

    def handle(self, *args, **opts):
        name = opts["name"].strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise CommandError("App name must be a valid Python identifier (lowercase).")

        base = Path(settings.BASE_DIR)
        app_dir = base / "apps" / name
        if app_dir.exists():
            raise CommandError(f"apps/{name} already exists.")

        cls = "".join(part.capitalize() for part in name.split("_"))
        verbose = name.replace("_", " ").title()

        # Package files.
        scaffold.write(app_dir / "__init__.py", "")
        scaffold.write(app_dir / "migrations" / "__init__.py", "")
        scaffold.write(app_dir / "apps.py", APPS_PY.format(cls=cls, name=name, verbose=verbose))
        scaffold.write(app_dir / "models.py", MODELS_PY.format(name=name))
        scaffold.write(app_dir / "serializers.py", scaffold.SERIALIZERS_HEADER)
        scaffold.write(app_dir / "views.py", scaffold.VIEWS_HEADER)
        scaffold.write(app_dir / "urls.py", scaffold.URLS_HEADER)
        scaffold.write(app_dir / "admin.py", scaffold.ADMIN_HEADER)

        # Register in INSTALLED_APPS.
        self._register_in_settings(base, name)

        self.stdout.write(self.style.SUCCESS(f"Created apps/{name} and registered it."))
        self.stdout.write(
            f"Next: add models to apps/{name}/models.py, then run "
            f"`python manage.py setup_model {name} <ModelName>`."
        )

    def _register_in_settings(self, base: Path, name: str) -> None:
        settings_path = base / "config" / "settings.py"
        content = scaffold.read(settings_path)
        entry = f'"apps.{name}",'
        if entry in content:
            return
        if scaffold.LOCAL_APPS_ANCHOR not in content:
            self.stdout.write(
                self.style.WARNING(
                    "Could not find the local-apps anchor in settings.py; "
                    f'add {entry} to INSTALLED_APPS manually.'
                )
            )
            return
        content = scaffold.insert_before_anchor(
            content, scaffold.LOCAL_APPS_ANCHOR, [entry]
        )
        scaffold.write(settings_path, content)

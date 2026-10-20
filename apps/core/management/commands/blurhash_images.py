"""Give every stored picture its blurhash and small copy (run once; safe to re-run).

    python manage.py blurhash_images            # hash what is missing
    python manage.py blurhash_images --dry-run  # only count

New uploads are hashed when uploaded, and anything else is hashed in the
background the first time it is served — this command just does the
existing pictures now instead of gradually.
"""
from django.apps import apps
from django.core.management.base import BaseCommand

from apps.core import blurhash
from apps.core.models import ImageHash


class Command(BaseCommand):
    help = "Compute blurhash previews for all stored pictures that lack one."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=0)

    def handle(self, *args, dry_run=False, limit=0, **opts):
        values: dict[str, str] = {}
        for model in apps.get_models():
            names = blurhash.picture_fields(model)
            if not names:
                continue
            mgr = model.objects.unscoped() if hasattr(model.objects, "unscoped") else model._base_manager
            for name in names:
                for v in mgr.exclude(**{name: ""}).exclude(**{f"{name}__isnull": True}).values_list(name, flat=True).distinct():
                    key = blurhash.identity(v)
                    if key and not blurhash._NOT_PICTURE.search(blurhash.strip(v).split("?", 1)[0]):
                        values.setdefault(key, blurhash.strip(v))
        have = set(
            ImageHash.objects.filter(key__in=list(values), thumb=blurhash.IN_DB).values_list("key", flat=True)
        )
        todo = [(k, v) for k, v in values.items() if k not in have]
        if limit:
            todo = todo[:limit]
        self.stdout.write(f"{len(values)} pictures, {len(have)} already done, {len(todo)} to do")
        if dry_run:
            return
        ok = failed = 0
        for i, (_key, value) in enumerate(todo, 1):
            if blurhash.compute(value):
                ok += 1
            else:
                failed += 1
            if i % 25 == 0 or i == len(todo):
                self.stdout.write(f"  {i}/{len(todo)} (ok {ok}, skipped {failed})")
        self.stdout.write(self.style.SUCCESS(f"Done: {ok} hashed, {failed} could not be read."))

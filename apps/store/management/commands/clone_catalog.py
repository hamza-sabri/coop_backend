from django.core.management.base import BaseCommand, CommandError

from apps.store import models
from apps.store.cloning import clone_catalog


class Command(BaseCommand):
    help = "Clone one store's catalogue (products + variants) into another."

    def add_arguments(self, parser):
        parser.add_argument("source")
        parser.add_argument("target")

    def _resolve(self, ref):
        ph = (
            models.Store.objects.filter(slug=ref).first()
            or (models.Store.objects.filter(pk=ref).first() if ref.isdigit() else None)
            or models.Store.objects.filter(name__icontains=ref).first()
        )
        if not ph:
            raise CommandError(f"Store not found: {ref}")
        return ph

    def handle(self, *args, **options):
        source = self._resolve(options["source"])
        target = self._resolve(options["target"])
        if source.pk == target.pk:
            raise CommandError("Source and target are the same store.")
        stats = clone_catalog(source, target)
        self.stdout.write(
            self.style.SUCCESS(f"Cloned {source} → {target}: {stats}")
        )

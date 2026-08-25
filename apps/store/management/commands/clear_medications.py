"""Wipe ONE store's product catalogue so it can be re-imported clean.

Why this exists: re-uploading a price list *updates existing rows by barcode*
rather than starting fresh, so months of test data and "scramble" leftovers
stick around. This command removes them first.

What it deletes (scoped to a single store via `.for_pharmacy()`):
    - that store's Medications
    - their MedicationVariants   (FK on_delete=CASCADE)
    - their MedicationImages     (FK on_delete=CASCADE)

What it PRESERVES (this is the whole point — sales/debts must survive):
    - every Sale / Debt and their line items. SaleItem.product /
      DebtItem.product (and .variant) are on_delete=SET_NULL, so the links
      go empty but each line keeps its frozen snapshot: name, unit price,
      category, quantity, line_total. Totals and history are untouched.

What it never touches:
    - this store's own Categories, Manufacturers and Customers — these are
      per-store (NOT shared), and the importer reuses them on re-upload.
    - the legacy `CatalogItem`/`CatalogItemImage` table — the one genuinely shared
      artifact, already inert on every tenant path and slated for removal in
      Phase C; nothing here reads or writes it.

Safe by construction: one tenant only, dry-run by default, single transaction.

    python manage.py clear_medications --store <slug>            # preview
    python manage.py clear_medications --store <slug> --confirm  # apply

Run --store with a wrong/blank slug once to have it print the slugs that
exist. After it finishes, re-upload the price list from the app as usual.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from apps.store import models


class Command(BaseCommand):
    help = (
        "Delete one store's products (+ variants + images) for a clean "
        "re-import. Keeps all sales and debts (links nulled, snapshots kept)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--store",
            required=True,
            help="Slug of the store whose catalogue to wipe.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Actually delete. Without it the command only previews (dry-run).",
        )

    def handle(self, *args, **opts):
        slug = opts["store"]
        store = models.Store.objects.filter(slug=slug).first()
        if not store:
            available = (
                ", ".join(
                    models.Store.objects.order_by("slug").values_list(
                        "slug", flat=True
                    )
                )
                or "(none)"
            )
            raise CommandError(
                f"No store with slug {slug!r}. Available slugs: {available}"
            )

        meds = models.Product.objects.for_pharmacy(store)
        med_count = meds.count()
        variant_count = models.ProductVariant.objects.for_pharmacy(store).count()
        image_count = models.ProductImage.objects.for_pharmacy(store).count()

        # Sale/Debt lines that will be DETACHED but KEPT (link -> NULL, snapshot
        # stays). Under tenant isolation every non-null link on this store's
        # lines points at a med/variant we're about to delete.
        sale_lines = (
            models.SaleItem.objects.for_pharmacy(store)
            .filter(Q(medication_id__isnull=False) | Q(variant_id__isnull=False))
            .count()
        )
        debt_lines = (
            models.DebtItem.objects.for_pharmacy(store)
            .filter(Q(medication_id__isnull=False) | Q(variant_id__isnull=False))
            .count()
        )

        self.stdout.write(
            f"Store: {store.name} [{store.slug}] (id={store.pk})"
        )
        self.stdout.write(
            f"  DELETE : {med_count} products, {variant_count} variants, "
            f"{image_count} product images"
        )
        self.stdout.write(
            f"  KEEP   : all sales & debts — {sale_lines} sale line(s) and "
            f"{debt_lines} debt line(s) keep their name/price/category snapshot "
            f"(product link cleared)"
        )
        self.stdout.write(
            "  KEPT   : this store's Categories, Manufacturers and Customers "
            "(per-store, not shared; reused on re-import)"
        )

        if not opts["confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY-RUN — nothing was deleted. Re-run with --confirm to apply."
                )
            )
            return

        with transaction.atomic():
            total, by_model = models.Product.objects.for_pharmacy(store).delete()

        self.stdout.write(
            self.style.SUCCESS(f"\nDeleted {total} row(s): {by_model or '{}'}")
        )
        remaining = models.Product.objects.for_pharmacy(store).count()
        self.stdout.write(
            f"Medications remaining for {store.slug}: {remaining}. "
            f"You can now re-upload the price list from the app."
        )

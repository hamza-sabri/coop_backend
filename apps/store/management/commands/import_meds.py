"""
Import the AL-Rahmah price list (Excel) into the Product catalogue.

    python manage.py import_meds                      # uses data/price-list.xlsx
    python manage.py import_meds --path /some/list.xlsx
    python manage.py import_meds --clear              # wipe meds, then re-import
    python manage.py import_meds --dry-run            # parse + report, write nothing

The sheet columns (Arabic headers) are mapped like this:

    الرقم            -> source_id   (stable id; used to skip already-imported rows)
    الاسم            -> name
    التكلفة          -> cost
    باركود           -> barcode
    العلامة التجارية -> brand
    الشركة المنتجة   -> manufacturer
    الرصيد الحالي    -> stock
    التصنيف          -> category
    ملاحظات          -> notes
    مفرق             -> price   (first number in e.g. "35 شيكل"; the retail price)

`image` is intentionally left blank — it's a URL you can fill later (or upload a
file to `image_file` on the API).

Idempotent: rows whose `source_id` already exists are skipped, so re-running only
adds what's new. Use --clear for a full refresh.
"""
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.store.models import Product

# Column indexes in the source sheet (0-based).
COL_SOURCE_ID = 0
COL_NAME = 2
COL_COST = 3
COL_BARCODE = 4
COL_BRAND = 5
COL_MANUFACTURER = 6
COL_STOCK = 7
COL_CATEGORY = 9
COL_NOTES = 14
COL_PRICE = 18

DEFAULT_PATH = Path(settings.BASE_DIR) / "data" / "price-list.xlsx"
_NUMBER = re.compile(r"[-+]?[0-9]*\.?[0-9]+")
# Largest value that fits DecimalField(max_digits=12, decimal_places=2).
# Some source cells hold a barcode where a price should be — those overflow, so
# we clamp them to 0 and keep the raw text in notes.
MAX_AMOUNT = Decimal("9999999999.99")


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _barcode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def _to_decimal(value) -> Decimal:
    """Best-effort Decimal from a number or a string like '20.44' / '35 شيكل'."""
    if value is None or value == "":
        return Decimal("0.00")
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return Decimal("0.00")
    match = _NUMBER.search(str(value))
    if not match:
        return Decimal("0.00")
    try:
        return Decimal(match.group()).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def _to_int(value) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    match = _NUMBER.search(str(value))
    return int(float(match.group())) if match else 0


class Command(BaseCommand):
    help = "Import products (with prices) from the AL-Rahmah price-list Excel file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path", default=str(DEFAULT_PATH),
            help=f"Path to the .xlsx file (default: {DEFAULT_PATH}).",
        )
        parser.add_argument(
            "--clear", action="store_true",
            help="Delete all existing products before importing.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Parse and report, but write nothing to the database.",
        )
        parser.add_argument(
            "--batch-size", type=int, default=1000,
            help="Rows per bulk insert (default: 1000).",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Only import the first N valid rows (0 = all).",
        )

    def handle(self, *args, **opts):
        try:
            import openpyxl
        except ImportError:
            raise CommandError("openpyxl is required: pip install openpyxl")

        path = Path(opts["path"])
        if not path.exists():
            raise CommandError(
                f"File not found: {path}\n"
                "Pass --path /path/to/price-list.xlsx (or place it at "
                f"{DEFAULT_PATH})."
            )

        if opts["clear"] and not opts["dry_run"]:
            deleted, _ = Product.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Cleared existing meds ({deleted} rows)."))

        existing_ids = set(
            Product.objects.exclude(source_id="").values_list("source_id", flat=True)
        )

        self.stdout.write(f"Reading {path} ...")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        next(rows, None)  # skip header

        batch, created, skipped_existing, skipped_blank = [], 0, 0, 0
        limit = opts["limit"]
        batch_size = opts["batch_size"]

        for raw in rows:
            if raw is None:
                continue
            name = _text(raw[COL_NAME])
            if not name:
                skipped_blank += 1
                continue

            source_id = _text(raw[COL_SOURCE_ID])
            if source_id and source_id in existing_ids:
                skipped_existing += 1
                continue
            if source_id:
                existing_ids.add(source_id)  # guard against in-file duplicates

            price_raw = raw[COL_PRICE]
            notes = _text(raw[COL_NOTES])

            price = _to_decimal(price_raw)
            price_clamped = price > MAX_AMOUNT  # a barcode landed in the price cell
            if price_clamped:
                price = Decimal("0.00")
            cost = _to_decimal(raw[COL_COST])
            if cost > MAX_AMOUNT:
                cost = Decimal("0.00")

            # Preserve the raw retail text whenever we couldn't keep it exactly:
            # tiered prices (several numbers) or an out-of-range value we zeroed.
            tiered = price_raw is not None and len(_NUMBER.findall(str(price_raw))) > 1
            if price_raw is not None and (tiered or price_clamped):
                tier = f"سعر مفرق: {_text(price_raw)}"
                notes = f"{notes}\n{tier}".strip() if notes else tier

            batch.append(Product(
                source_id=source_id,
                name=name,
                barcode=_barcode(raw[COL_BARCODE]),
                price=price,
                cost=cost,
                brand=_text(raw[COL_BRAND]),
                manufacturer=_text(raw[COL_MANUFACTURER]),
                category=_text(raw[COL_CATEGORY]),
                stock=_to_int(raw[COL_STOCK]),
                notes=notes,
                image="",
            ))

            if limit and (created + len(batch)) >= limit:
                batch = batch[: max(0, limit - created)]
                created += self._flush(batch, batch_size, opts["dry_run"])
                batch = []
                break

            if len(batch) >= batch_size:
                created += self._flush(batch, batch_size, opts["dry_run"])
                batch = []

        if batch:
            created += self._flush(batch, batch_size, opts["dry_run"])

        wb.close()
        verb = "Would import" if opts["dry_run"] else "Imported"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {created} meds. "
            f"Skipped {skipped_existing} already-present, {skipped_blank} blank-name."
        ))
        if not opts["dry_run"]:
            self.stdout.write(f"Total meds in DB: {Product.objects.count()}")

    def _flush(self, batch, batch_size, dry_run) -> int:
        if not batch:
            return 0
        if dry_run:
            return len(batch)
        Product.objects.bulk_create(batch, batch_size=batch_size)
        return len(batch)

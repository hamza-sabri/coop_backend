"""Seed the SHARED product catalog from a harvested JSON file.

Populates the tenant-neutral `CatalogItem` table (barcode → default name + default
images) so that when any store scans a barcode, the name and photo can
auto-fill. This touches ONLY the shared catalog — never any store's data.

The JSON is what the urpharmacy harvest produces:
    { "products": [ { "barcode","name","categories","brand","images",... } ] }

Only rows with a valid barcode are used (a shared CatalogItem is keyed by barcode).
Idempotent: existing products are left as-is unless --update is passed; images
are only added when a product has none. By default image URLs are stored as
external references (no downloading). Pass --download to copy them into your
own storage (B2/local) — do this ONLY once you have permission to reuse the
photos.

Run:
    python manage.py seed_shared_catalog urpharmacy-catalog.json
    python manage.py seed_shared_catalog urpharmacy-catalog.json --download
    python manage.py seed_shared_catalog urpharmacy-catalog.json --limit 50   # trial
"""
import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.store import models

BARCODE_RE = re.compile(r"^\d{8,14}$")


def clean_barcode(value) -> str:
    v = re.sub(r"\D+$", "", str(value or "").strip())  # trailing dots etc.
    return v if BARCODE_RE.match(v) else ""


class Command(BaseCommand):
    help = "Seed the shared CatalogItem catalog from a harvested JSON file."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the harvested JSON file.")
        parser.add_argument("--limit", type=int, default=0, help="Only process N rows (trial run).")
        parser.add_argument("--update", action="store_true", help="Overwrite name/images on existing products.")
        parser.add_argument("--max-images", type=int, default=4, help="Max images kept per product.")
        parser.add_argument(
            "--download",
            action="store_true",
            help="Download images into our own storage (needs permission!). "
            "Default stores the source URLs as references.",
        )

    def handle(self, *args, **opts):
        path = Path(opts["path"])
        if not path.exists():
            self.stderr.write(f"File not found: {path}")
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        products = data.get("products", data if isinstance(data, list) else [])
        if opts["limit"]:
            products = products[: opts["limit"]]

        store = None
        if opts["download"]:
            from apps.core.uploads import store_upload  # noqa: F401

            store = self._make_downloader()

        created = updated = skipped_no_barcode = skipped_exists = img_added = 0
        seen = set()

        for row in products:
            barcode = clean_barcode(row.get("barcode"))
            name = " ".join(str(row.get("name") or "").split())
            if not barcode or not name:
                skipped_no_barcode += 1
                continue
            if barcode in seen:
                continue
            seen.add(barcode)

            images = [u for u in (row.get("images") or []) if u][: opts["max_images"]]
            product, was_created = models.CatalogItem.objects.get_or_create(
                barcode=barcode,
                defaults={"name": name, "image": ""},
            )
            if was_created:
                created += 1
            elif opts["update"]:
                product.name = name
                updated += 1
            else:
                skipped_exists += 1
                continue

            # Main image + gallery.
            urls = [store(u) if store else u for u in images]
            urls = [u for u in urls if u]
            if urls and (not product.image or opts["update"]):
                product.image = urls[0]
            product.save()
            if not product.images.exists():
                for pos, u in enumerate(urls[1:], start=1):
                    product.images.create(image=u, position=pos)
                    img_added += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Shared catalog seeded: created={created}, updated={updated}, "
                f"existing_skipped={skipped_exists}, no_barcode_skipped={skipped_no_barcode}, "
                f"gallery_images_added={img_added}. Total Products now: "
                f"{models.CatalogItem.objects.count()}."
            )
        )

    def _make_downloader(self):
        """Return a fn(url)->stored_url that copies an image into our storage."""
        import urllib.request
        from django.core.files.base import ContentFile
        from apps.core.uploads import store_upload

        def download(url):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    content = resp.read()
                ext = Path(url.split("?")[0]).suffix or ".jpg"
                f = ContentFile(content, name=f"seed{ext}")
                return store_upload(f, "products")
            except Exception:  # noqa: BLE001
                return url  # fall back to the reference URL

        return download

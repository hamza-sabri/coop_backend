"""Public marketing-stats endpoint — aggregate only, no auth, tenant-safe.

Run: python manage.py test apps.store.tests.test_public_stats
"""
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models


class PublicStatsTests(TestCase):
    def setUp(self):
        cache.clear()

    def _stats(self):
        cache.clear()  # the view caches; recompute for each assertion point
        r = APIClient().get("/api/v1/public/stats/")  # no authentication
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_stats_are_public_and_aggregate(self):
        base = self._stats()
        for key in ("products", "listings", "stores", "categories"):
            self.assertIn(key, base)

        active = models.Store.objects.create(name="A", slug="ps-active")
        inactive = models.Store.objects.create(
            name="B", slug="ps-inactive", is_active=False
        )
        cat_a = models.Category.objects.create(store=active, name="زد-تصنيف-فريد")
        cat_b = models.Category.objects.create(store=inactive, name="زد-تصنيف-فريد")
        # "products" now counts DISTINCT barcodes across listings — the shared
        # CatalogItem table is being retired and no longer feeds public stats.
        models.Product.objects.create(
            store=active, name="M1", price=Decimal("5"), category=cat_a, barcode="psb1"
        )
        models.Product.objects.create(
            store=active, name="M2", price=Decimal("6"), category=cat_a, barcode="psb2"
        )
        # Same barcode in another store → still ONE distinct product.
        models.Product.objects.create(
            store=inactive, name="M3", price=Decimal("7"), category=cat_b, barcode="psb1"
        )

        d = self._stats()
        # Deltas isolate our rows from any migration-seeded data.
        self.assertEqual(d["products"], base["products"] + 2)
        self.assertEqual(d["listings"], base["listings"] + 3)
        self.assertEqual(d["stores"], base["stores"] + 1)  # inactive excluded
        # Category coverage merges the same NAME across tenants (2 + 1 = 3).
        mine = [c for c in d["categories"] if c["name"] == "زد-تصنيف-فريد"]
        self.assertEqual(mine and mine[0]["count"], 3)
        # No tenant-identifying data leaks.
        blob = str(d)
        for leak in ("slug", "ps-active", "ps-inactive", "price", "stock"):
            self.assertNotIn(leak, blob)

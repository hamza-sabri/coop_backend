"""Cross-store catalogue clone + tenant-scoped bulk delete (flush).

Run: python manage.py test apps.store.tests.test_catalog_ops
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models
from apps.store.cloning import clone_catalog

User = get_user_model()


class CloneCatalogTests(TestCase):
    def setUp(self):
        self.src = models.Store.objects.create(name="Src", slug="cln-src")
        self.dst = models.Store.objects.create(name="Dst", slug="cln-dst")

    def test_clone_copies_meds_variants_prices_attributes(self):
        med = models.Product.objects.create(
            store=self.src,
            name="Panadol",
            barcode="P1",
            price=Decimal("10.00"),
            cost=Decimal("6.00"),
            stock=Decimal("3"),
            attributes={"note": "x"},
        )
        models.ProductVariant.objects.create(
            product=med,
            label="أحمر / L",
            price=Decimal("12.00"),
            attributes={"اللون": "أحمر", "الحجم": "L"},
        )
        stats = clone_catalog(self.src, self.dst)
        self.assertEqual(stats["created"], 1)
        self.assertEqual(stats["variants"], 1)
        cloned = models.Product.objects.unscoped().get(store=self.dst, barcode="P1")
        self.assertEqual(cloned.price, Decimal("10.00"))
        self.assertEqual(cloned.attributes, {"note": "x"})
        variant = cloned.variants.get()
        self.assertEqual(variant.label, "أحمر / L")
        self.assertEqual(variant.price, Decimal("12.00"))
        self.assertEqual(variant.attributes, {"اللون": "أحمر", "الحجم": "L"})
        # The retired shared catalog is never touched by cloning.
        self.assertIsNone(cloned.catalog_item_id)
        self.assertEqual(models.CatalogItem.objects.count(), 0)
        self.assertEqual(models.Product.objects.unscoped().filter(store=self.src).count(), 1)

    def test_clone_is_idempotent(self):
        models.Product.objects.create(
            store=self.src, name="A", barcode="B1", price=Decimal("5")
        )
        clone_catalog(self.src, self.dst)
        clone_catalog(self.src, self.dst)
        self.assertEqual(models.Product.objects.unscoped().filter(store=self.dst).count(), 1)


class BulkDeleteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ph = models.Store.objects.create(name="Mine", slug="mine")
        self.other = models.Store.objects.create(name="Other", slug="other")
        self.user = User.objects.create_user("me", password="x", store=self.ph)
        self.c = APIClient()
        self.c.force_authenticate(self.user)

    def test_bulk_delete_by_ids_scoped_and_keeps_legacy_shared_product(self):
        # Legacy shared row (pre-isolation) — deleting a med must not cascade.
        legacy = models.CatalogItem.objects.create(barcode="X1", name="M1")
        mine = models.Product.objects.create(
            store=self.ph, name="M1", barcode="X1", price=Decimal("1"),
            catalog_item=legacy,
        )
        models.ProductVariant.objects.create(
            product=mine, label="v", price=Decimal("1")
        )
        theirs = models.Product.objects.create(
            store=self.other, name="M2", price=Decimal("1")
        )
        r = self.c.post(
            "/api/v1/products/bulk_delete/",
            {"ids": [mine.id, theirs.id]},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], 1)
        self.assertFalse(models.Product.objects.unscoped().filter(pk=mine.id).exists())
        self.assertTrue(models.Product.objects.unscoped().filter(pk=theirs.id).exists())
        self.assertTrue(models.CatalogItem.objects.filter(barcode="X1").exists())

    def test_bulk_delete_all_flushes_only_own(self):
        models.Product.objects.create(store=self.ph, name="A", price=Decimal("1"))
        models.Product.objects.create(store=self.ph, name="B", price=Decimal("1"))
        models.Product.objects.create(
            store=self.other, name="C", price=Decimal("1")
        )
        r = self.c.post("/api/v1/products/bulk_delete/", {"all": True}, format="json")
        self.assertEqual(r.json()["deleted"], 2)
        self.assertEqual(models.Product.objects.unscoped().filter(store=self.ph).count(), 0)
        self.assertEqual(models.Product.objects.unscoped().filter(store=self.other).count(), 1)

    def test_bulk_delete_requires_ids_or_all(self):
        r = self.c.post("/api/v1/products/bulk_delete/", {}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_seed_demo_clones_richest_into_own_empty_pharmacy(self):
        rich = models.Store.objects.create(name="Rich", slug="rich")
        med = models.Product.objects.create(
            store=rich, name="Panadol", barcode="Z1", price=Decimal("9")
        )
        models.ProductVariant.objects.create(
            product=med, label="v", price=Decimal("9")
        )
        r = self.c.post("/api/v1/products/seed_demo/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["created"], 1)
        self.assertEqual(models.Product.objects.unscoped().filter(store=self.ph).count(), 1)
        self.assertEqual(
            models.Product.objects.unscoped().get(store=self.ph, barcode="Z1")
            .variants.count(),
            1,
        )

    def test_flush_all_preserves_history_and_other_tenants(self):
        """The delete-all-and-reimport flow: store A flushes its whole
        catalogue. Every A product/variant/image must go; store B's
        catalogue must be untouched; and A's sales/debts history must survive
        with its snapshots (name/price/totals) intact — FKs go NULL, rows stay.
        """
        # --- Store A (self.ph): two meds, variants, gallery images.
        med1 = models.Product.objects.create(
            store=self.ph, name="Panadol", barcode="A1",
            price=Decimal("10.00"), stock=Decimal("50"),
        )
        med2 = models.Product.objects.create(
            store=self.ph, name="Brufen", barcode="A2",
            price=Decimal("7.50"), stock=Decimal("20"),
        )
        var1 = models.ProductVariant.objects.create(
            product=med1, label="500mg", price=Decimal("12.00"),
            stock=Decimal("30"),
        )
        models.ProductVariant.objects.create(
            product=med2, label="400mg", price=Decimal("8.00")
        )
        models.ProductImage.objects.create(
            product=med1, image="https://x/1.jpg"
        )
        models.ProductImage.objects.create(
            product=med2, image="https://x/2.jpg", position=1
        )
        # --- Store B: its own med + variant + image, must stay untouched.
        med_b = models.Product.objects.create(
            store=self.other, name="Aspirin", barcode="B1",
            price=Decimal("3.00"),
        )
        models.ProductVariant.objects.create(
            product=med_b, label="100mg", price=Decimal("3.50")
        )
        models.ProductImage.objects.create(
            product=med_b, image="https://x/b.jpg"
        )

        # --- A sells med1 (variant) + med2 through the real API so the
        # snapshot fields are populated exactly as production does.
        r = self.c.post(
            "/api/v1/sales/",
            {
                "payment_method": "cash",
                "items": [
                    {"product": med1.pk, "variant": var1.pk, "quantity": 2},
                    {"product": med2.pk, "quantity": 1},
                ],
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        sale_id = r.json()["id"]
        sale_total = models.Sale.objects.for_pharmacy(self.ph.pk).get(pk=sale_id).total

        # --- A records a debt referencing med1.
        cust = models.Customer.objects.create(store=self.ph, name="زبون")
        r = self.c.post(
            "/api/v1/debts/",
            {
                "customer": cust.pk,
                "items": [{"product": med1.pk, "quantity": 3}],
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        debt_id = r.json()["id"]
        debt_total = models.Debt.objects.for_pharmacy(self.ph.pk).get(pk=debt_id).total

        # --- Flush everything as A.
        r = self.c.post("/api/v1/products/bulk_delete/", {"all": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["deleted"], 2)

        # A's catalogue is completely gone — meds, variants, gallery images.
        self.assertEqual(models.Product.objects.for_pharmacy(self.ph.pk).count(), 0)
        self.assertEqual(
            models.ProductVariant.objects.for_pharmacy(self.ph.pk).count(), 0
        )
        self.assertEqual(
            models.ProductImage.objects.for_pharmacy(self.ph.pk).count(), 0
        )

        # B is fully untouched.
        self.assertEqual(models.Product.objects.for_pharmacy(self.other.pk).count(), 1)
        self.assertEqual(
            models.ProductVariant.objects.for_pharmacy(self.other.pk).count(), 1
        )
        self.assertEqual(
            models.ProductImage.objects.for_pharmacy(self.other.pk).count(), 1
        )

        # A's sale survives: FKs nulled, snapshots + totals intact.
        sale = models.Sale.objects.for_pharmacy(self.ph.pk).get(pk=sale_id)
        self.assertEqual(sale.total, sale_total)
        items = list(sale.items.order_by("id"))
        self.assertEqual(len(items), 2)
        self.assertIsNone(items[0].product_id)
        self.assertIsNone(items[0].variant_id)
        self.assertEqual(items[0].medication_name, "Panadol")
        self.assertEqual(items[0].variant_label, "500mg")
        self.assertEqual(items[0].unit_price, Decimal("12.00"))
        self.assertEqual(items[0].line_total, Decimal("24.00"))
        self.assertIsNone(items[1].product_id)
        self.assertEqual(items[1].medication_name, "Brufen")
        self.assertEqual(items[1].unit_price, Decimal("7.50"))
        self.assertEqual(items[1].line_total, Decimal("7.50"))

        # A's debt survives the same way.
        debt = models.Debt.objects.for_pharmacy(self.ph.pk).get(pk=debt_id)
        self.assertEqual(debt.total, debt_total)
        d_item = debt.items.get()
        self.assertIsNone(d_item.product_id)
        self.assertEqual(d_item.medication_name, "Panadol")
        self.assertEqual(d_item.unit_price, Decimal("10.00"))
        self.assertEqual(d_item.line_total, Decimal("30.00"))

    def test_flush_all_invalidates_pos_catalog_cache_and_version(self):
        from apps.store.views import catalog_version_key, pos_catalog_key

        models.Product.objects.create(
            store=self.ph, name="A", price=Decimal("1")
        )
        # Prime the POS catalogue cache + version fingerprint.
        r = self.c.get("/api/v1/products/pos_catalog/")
        self.assertEqual(r.status_code, 200)
        r = self.c.get("/api/v1/products/catalog_version/")
        self.assertEqual(r.status_code, 200)
        v_before = r.json()["version"]
        self.assertIsNotNone(cache.get(pos_catalog_key(self.ph.pk)))

        self.c.post("/api/v1/products/bulk_delete/", {"all": True}, format="json")

        # Both keys must be gone so devices refetch and see the empty list.
        self.assertIsNone(cache.get(pos_catalog_key(self.ph.pk)))
        self.assertIsNone(cache.get(catalog_version_key(self.ph.pk)))
        v_after = self.c.get("/api/v1/products/catalog_version/").json()["version"]
        self.assertNotEqual(v_before, v_after)

    def test_seed_demo_refused_when_catalog_not_empty(self):
        models.Store.objects.create(name="Rich2", slug="rich2")
        models.Product.objects.create(
            store=models.Store.objects.get(slug="rich2"),
            name="Y",
            price=Decimal("1"),
        )
        models.Product.objects.create(store=self.ph, name="Mine", price=Decimal("1"))
        r = self.c.post("/api/v1/products/seed_demo/", {}, format="json")
        self.assertEqual(r.status_code, 400)

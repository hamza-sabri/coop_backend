"""Public price-check search — live suggestions by name / partial barcode,
tenant-scoped, variant-aware, and privacy-safe (no cost or stock counts leak).

Run: python manage.py test apps.store.tests.test_public_price_search
"""
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models


class PublicPriceSearchTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.store = models.Store.objects.create(name="A", slug="pps-a")
        self.other = models.Store.objects.create(name="B", slug="pps-b")

    def _search(self, q, slug="pps-a"):
        cache.clear()
        r = self.client.get(
            f"/api/v1/public/price-check/?store={slug}&q={q}"
        )
        self.assertEqual(r.status_code, 200)
        return r.json().get("results", [])

    def _lookup(self, barcode, slug="pps-a"):
        cache.clear()
        r = self.client.get(
            f"/api/v1/public/price-check/?store={slug}&barcode={barcode}"
        )
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_search_by_name_returns_priced_med_public_fields_only(self):
        models.Product.objects.create(
            store=self.store,
            name="Paracetamol 500",
            price=Decimal("12.50"),
            cost=Decimal("7.00"),
            stock=Decimal("30"),
            barcode="PARA500",
        )
        results = self._search("paracet")
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row["name"], "Paracetamol 500")
        self.assertEqual(row["price"], "12.50")
        # No cost or stock ever crosses the public boundary.
        blob = str(row)
        self.assertNotIn("7.00", blob)
        self.assertNotIn("30", blob)
        self.assertNotIn("cost", blob)
        self.assertNotIn("stock", blob)

    def test_search_by_partial_barcode(self):
        models.Product.objects.create(
            store=self.store, name="Aspirin", price=Decimal("5"), barcode="6221000123"
        )
        self.assertEqual(len(self._search("62210")), 1)
        self.assertEqual(len(self._search("999")), 0)

    def test_scan_returns_video_and_gallery(self):
        """A barcode scan surfaces the product's video and gallery photos, not
        just the price — so the shopper can watch/flip through them."""
        med = models.Product.objects.create(
            store=self.store,
            name="Norvasc 10mg",
            price=Decimal("19"),
            barcode="7290013592415",
            video_url="https://youtu.be/abc123",
        )
        models.ProductImage.objects.create(
            product=med, image="https://img.example/1.jpg", position=0
        )
        models.ProductImage.objects.create(
            product=med, image="https://img.example/2.jpg", position=1
        )
        r = self._lookup("7290013592415")
        self.assertTrue(r["found"])
        self.assertIn("abc123", r["video_url"])
        self.assertEqual(len(r["images"]), 2)
        self.assertIn("1.jpg", r["images"][0])

    def test_unpriced_med_without_variants_is_hidden(self):
        models.Product.objects.create(
            store=self.store, name="Ghost Item", price=Decimal("0")
        )
        self.assertEqual(self._search("ghost"), [])

    def test_search_variants_default_to_product_price(self):
        med = models.Product.objects.create(
            store=self.store, name="Syrup", price=Decimal("10")
        )
        models.ProductVariant.objects.create(
            product=med, label="Cherry", price=Decimal("12"), stock=Decimal("5")
        )
        # No price of its own → shown at the product's price (10), not hidden.
        models.ProductVariant.objects.create(
            product=med, label="Lemon", price=Decimal("0")
        )
        # Inactive variant is still excluded.
        models.ProductVariant.objects.create(
            product=med, label="Old", price=Decimal("9"), is_active=False
        )
        results = self._search("syrup")
        self.assertEqual(len(results), 1)
        by_label = {v["label"]: v["price"] for v in results[0]["variants"]}
        self.assertEqual(set(by_label), {"Cherry", "Lemon"})
        self.assertEqual(by_label["Cherry"], "12.00")
        self.assertEqual(by_label["Lemon"], "10.00")

    def test_variant_attributes_are_exposed(self):
        med = models.Product.objects.create(
            store=self.store, name="Shirt", price=Decimal("10")
        )
        models.ProductVariant.objects.create(
            product=med,
            label="أحمر / S",
            price=Decimal("12"),
            attributes={"اللون": "أحمر", "الحجم": "S"},
        )
        results = self._search("shirt")
        variant = results[0]["variants"][0]
        self.assertEqual(variant["attributes"], {"اللون": "أحمر", "الحجم": "S"})

    def test_variant_only_product_surfaces_with_null_price(self):
        med = models.Product.objects.create(
            store=self.store, name="Kit", price=Decimal("0")
        )
        models.ProductVariant.objects.create(
            product=med, label="Large", price=Decimal("20"), stock=Decimal("3")
        )
        results = self._search("kit")
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["price"])
        self.assertEqual(len(results[0]["variants"]), 1)

    def test_tenant_isolation(self):
        models.Product.objects.create(
            store=self.other, name="Foreign Med", price=Decimal("5")
        )
        self.assertEqual(self._search("foreign"), [])
        self.assertEqual(self._search("foreign", slug="pps-b").__len__(), 1)

    def test_exact_variant_barcode_resolves_to_variant(self):
        med = models.Product.objects.create(
            store=self.store, name="Cream", price=Decimal("8")
        )
        models.ProductVariant.objects.create(
            product=med,
            label="50g",
            barcode="CREAM50",
            price=Decimal("14"),
            stock=Decimal("4"),
        )
        data = self._lookup("CREAM50")
        self.assertTrue(data["found"])
        self.assertEqual(data["price"], "14.00")
        self.assertIn("50g", data["name"])

    def test_exact_med_barcode_includes_variants(self):
        med = models.Product.objects.create(
            store=self.store, name="Tonic", price=Decimal("9"), barcode="TONIC1"
        )
        models.ProductVariant.objects.create(
            product=med, label="Grape", price=Decimal("11"), stock=Decimal("2")
        )
        data = self._lookup("TONIC1")
        self.assertTrue(data["found"])
        self.assertEqual(data["price"], "9.00")
        self.assertEqual(len(data.get("variants", [])), 1)

    def test_wrong_pharmacy_returns_empty(self):
        r = self.client.get("/api/v1/public/price-check/?store=nope&q=x")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json().get("results", []), [])

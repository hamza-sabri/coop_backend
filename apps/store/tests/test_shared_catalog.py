"""The shared product catalog is RETIRED (tenant isolation, plan §3.5).

The rules under test:
- nothing a store writes (API create/edit, import) ever creates or links
  a shared CatalogItem row — each catalogue is self-contained per store
- no name prefill, no image fallback from other tenants' data — ever
- legacy CatalogItem rows still in the DB (until Phase C drops them) are inert
"""
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()


def png(name="x.png"):
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buf, "PNG")
    buf.seek(0)
    buf.name = name
    return buf


class SharedCatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph_a = models.Store.objects.create(name="A", slug="cat-a")
        cls.ph_b = models.Store.objects.create(name="B", slug="cat-b")
        cls.user_a = User.objects.create_user("cat_a", password="x", store=cls.ph_a)
        cls.user_b = User.objects.create_user("cat_b", password="x", store=cls.ph_b)

    def setUp(self):
        cache.clear()
        self.A = APIClient()
        self.A.force_authenticate(self.user_a)
        self.B = APIClient()
        self.B.force_authenticate(self.user_b)
        self.anon = APIClient()

    def test_import_updates_own_listing_and_never_writes_shared_catalog(self):
        """The import contract, per store:
        - barcode already listed by THIS store → update its price/name/etc
          in place (no duplicate row)
        - barcode not listed → create the listing for THIS store only —
          NO shared CatalogItem row is created or linked, ever.
        """
        from apps.store.importers import import_products

        # B already lists the barcode with a LEGACY shared-catalog link.
        legacy = models.CatalogItem.objects.create(barcode="9001", name="اسم صيدلية ب")
        med_b = models.Product.objects.create(
            store=self.ph_b, name="اسم صيدلية ب", barcode="9001",
            price=Decimal("50.00"), stock=4, catalog_item=legacy,
        )

        row = {
            "row": 2, "name": "اسم صيدلية أ", "barcode": "9001",
            "source_id": "", "price": Decimal("12.00"), "cost": None,
            "stock": 7, "category": "", "manufacturer": "",
        }
        # A imports the same barcode → NEW listing for A, fully independent.
        stats = import_products(self.ph_a, [dict(row)])
        self.assertEqual(stats, {"created": 1, "updated": 0})
        med_a = models.Product.objects.unscoped().get(store=self.ph_a, barcode="9001")
        self.assertIsNone(med_a.catalog_item_id)  # never linked to shared data
        self.assertEqual(med_a.price, Decimal("12.00"))
        med_b.refresh_from_db()
        self.assertEqual((med_b.name, med_b.price), ("اسم صيدلية ب", Decimal("50.00")))
        self.assertEqual(med_b.catalog_item_id, legacy.pk)  # legacy link untouched

        # A re-imports with a new price/name → same row UPDATED, no duplicate.
        row.update(name="اسم أحدث", price=Decimal("14.50"))
        stats = import_products(self.ph_a, [dict(row)])
        self.assertEqual(stats, {"created": 0, "updated": 1})
        self.assertEqual(
            models.Product.objects.unscoped().filter(store=self.ph_a, barcode="9001").count(), 1
        )
        med_a.refresh_from_db()
        self.assertEqual((med_a.name, med_a.price), ("اسم أحدث", Decimal("14.50")))
        self.assertIsNone(med_a.catalog_item_id)
        self.assertEqual(models.CatalogItem.objects.count(), 1)  # nothing new

    def test_api_create_never_creates_or_links_shared_product(self):
        r = self.A.post(
            "/api/v1/products/",
            {"name": "Panadol Extra", "barcode": "729111", "price": "10"},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(models.CatalogItem.objects.count(), 0)
        self.assertIsNone(r.json()["catalog_item"])

        # store B lists the same barcode — its own independent row
        r = self.B.post(
            "/api/v1/products/",
            {"name": "بنادول اكسترا", "barcode": "729111", "price": "12"},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(models.CatalogItem.objects.count(), 0)
        self.assertIsNone(r.json()["catalog_item"])
        self.assertEqual(r.json()["name"], "بنادول اكسترا")

    def test_name_never_prefilled_from_another_pharmacy(self):
        """Isolation: creating without a name is rejected even when another
        store already listed the barcode — their name must never leak."""
        self.A.post(
            "/api/v1/products/",
            {"name": "Vitamin C 1000", "barcode": "888", "price": "20"},
            format="json",
        )
        r = self.B.post(
            "/api/v1/products/", {"barcode": "888", "price": "22"}, format="json"
        )
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("Vitamin C 1000", r.content.decode())
        # unknown barcode without a name is rejected too
        r = self.B.post(
            "/api/v1/products/", {"barcode": "000new", "price": "1"}, format="json"
        )
        self.assertEqual(r.status_code, 400)

    def test_image_never_falls_back_to_another_pharmacys_photo(self):
        """Isolation: B listing the same barcode with no photo sees NO photo —
        A's picture must never appear in B's catalogue."""
        r = self.A.post(
            "/api/v1/products/",
            {"name": "Med", "barcode": "555", "price": "5", "image_file": png()},
            format="multipart",
        )
        a_image = r.json()["image"]
        self.assertTrue(a_image)
        r = self.B.post(
            "/api/v1/products/",
            {"name": "Med B", "barcode": "555", "price": "9"},
            format="json",
        )
        self.assertEqual(r.json()["image"], "", "B must NOT inherit A's photo")
        med_b_id = r.json()["id"]
        self.assertEqual(models.Product.objects.unscoped().get(pk=med_b_id).image, "")
        # B uploads their own → theirs shows, A's untouched
        r = self.B.patch(
            f"/api/v1/products/{med_b_id}/",
            {"image_file": png("own.png")},
            format="multipart",
        )
        self.assertNotEqual(r.json()["image"], "")
        med_a = models.Product.objects.unscoped().get(store=self.ph_a, barcode="555")
        self.assertTrue(med_a.image)

    def test_public_price_check_shows_own_price_and_never_anothers_image(self):
        self.A.post(
            "/api/v1/products/",
            {"name": "Med", "barcode": "777", "price": "10", "image_file": png()},
            format="multipart",
        )
        self.B.post(
            "/api/v1/products/",
            {"name": "Med B", "barcode": "777", "price": "33"},
            format="json",
        )
        rb = self.anon.get(
            "/api/v1/public/price-check/?store=cat-b&barcode=777"
        ).json()
        self.assertEqual(rb["price"], "33.00")  # B's own price
        self.assertEqual(rb["image"], "")  # NOT A's photo
        self.assertEqual(set(rb.keys()), {"found", "name", "price", "image"})

    def test_barcode_edits_never_touch_the_shared_catalog(self):
        r = self.A.post(
            "/api/v1/products/",
            {"name": "Med", "barcode": "111", "price": "5"},
            format="json",
        )
        med_id = r.json()["id"]
        r = self.A.patch(
            f"/api/v1/products/{med_id}/", {"barcode": "222"}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        med = models.Product.objects.unscoped().get(pk=med_id)
        self.assertEqual(med.barcode, "222")
        self.assertIsNone(med.catalog_item_id)
        self.assertEqual(models.CatalogItem.objects.count(), 0)

    def test_no_products_endpoint_exists(self):
        # The SHARED catalog (CatalogItem) must have no HTTP surface at all —
        # exposing it would leak which barcodes other stores list.
        #
        # In the pharmacy original this contract was checked against
        # "/api/v1/products/", because there `products` WAS the shared catalog
        # and `medications` was the tenant's own listing. The template renamed
        # Medication -> Product, so "/api/v1/products/" is now the tenant's own
        # listing endpoint and legitimately answers 200 (the very next test
        # POSTs to it). The contract is unchanged; only the path it lives at
        # moved. Assert it against the shared catalog's own name.
        self.assertEqual(self.A.get("/api/v1/catalog-items/").status_code, 404)
        self.assertEqual(self.anon.get("/api/v1/catalog-items/").status_code, 404)

    def test_med_response_exposes_no_cross_tenant_hints(self):
        self.A.post(
            "/api/v1/products/",
            {"name": "Secret Stock", "barcode": "999", "price": "10"},
            format="json",
        )
        r = self.B.post(
            "/api/v1/products/",
            {"name": "My Own Name", "barcode": "999", "price": "1"},
            format="json",
        )
        body = r.json()
        # product_name stays in the payload (API shape) but never carries
        # another store's donated name.
        self.assertEqual(body["product_name"], "")
        self.assertNotIn("Secret Stock", str(body))
        for forbidden in ("listings", "stores", "pharmacy_count"):
            self.assertNotIn(forbidden, body)

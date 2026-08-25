"""THE tenant-isolation CONTRACT — permanent, never delete (plan §3.5).

Two stores list the SAME barcode as two DIFFERENT products (different
name, price, photo — exactly the real-world Hesabate case: 8001090595614 is
one product in alrahmah and another in alhaya). Every read path must return
ONLY the requesting tenant's row, in BOTH directions:

- staff product search + exact barcode filter
- POS catalog (the offline scan source of truth)
- public price page (anonymous, slug-scoped): exact barcode + typed search
- a barcode listed by only ONE store is NOT FOUND in the other

It also pins the legacy-leak regression: rows that still carry a link to the
old shared CatalogItem table (with a donated photo) must never surface that
shared data to another tenant. If any test here fails, DO NOT SHIP.

Run: python manage.py test apps.store.tests.test_tenant_contract
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()

SHARED_BARCODE = "8001090595614"  # listed by BOTH stores (different item)
ONLY_A_BARCODE = "6291041500213"  # listed by A only
ONLY_B_BARCODE = "7290002193067"  # listed by B only


class TenantContractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph_a = models.Store.objects.create(name="صيدلية أ", slug="ctr-a")
        cls.ph_b = models.Store.objects.create(name="صيدلية ب", slug="ctr-b")
        cls.user_a = User.objects.create_user("ctr_a", password="x", store=cls.ph_a)
        cls.user_b = User.objects.create_user("ctr_b", password="x", store=cls.ph_b)

        # LEGACY shared-catalog row (pre-isolation data still in the DB until
        # Phase C): carries a photo donated by store A.
        cls.legacy_product = models.CatalogItem.objects.create(
            barcode=SHARED_BARCODE,
            name="ALWAYS DISCREET لارج",
            image="https://cdn.example/shared-donated-by-a.png",
        )
        models.CatalogItemImage.objects.create(
            catalog_item=cls.legacy_product,
            image="https://cdn.example/shared-gallery.png",
        )

        cls.med_a = models.Product.objects.create(
            store=cls.ph_a,
            catalog_item=cls.legacy_product,  # legacy link — must be inert on reads
            name="ALWAYS DISCREET لارج",
            barcode=SHARED_BARCODE,
            price=Decimal("25.00"),
            stock=Decimal("5"),
            image="https://cdn.example/a-own.png",
        )
        # B: SAME barcode, different product, NO photo of its own — the exact
        # setup where the old shared-image fallback used to leak A's picture.
        cls.med_b = models.Product.objects.create(
            store=cls.ph_b,
            catalog_item=cls.legacy_product,
            name="always discreet pants 8 pcs",
            barcode=SHARED_BARCODE,
            price=Decimal("39.00"),
            stock=Decimal("2"),
            image="",
        )
        cls.only_a = models.Product.objects.create(
            store=cls.ph_a, name="Only In A", barcode=ONLY_A_BARCODE,
            price=Decimal("7.00"), stock=Decimal("3"),
        )
        cls.only_b = models.Product.objects.create(
            store=cls.ph_b, name="Only In B", barcode=ONLY_B_BARCODE,
            price=Decimal("8.00"), stock=Decimal("3"),
        )

    def setUp(self):
        cache.clear()
        self.A = APIClient()
        self.A.force_authenticate(self.user_a)
        self.B = APIClient()
        self.B.force_authenticate(self.user_b)
        self.anon = APIClient()

    # ------------------------------------------------------------------ staff

    def test_staff_search_returns_only_own_row_for_shared_barcode(self):
        for client, own, other in (
            (self.A, "ALWAYS DISCREET لارج", "always discreet pants 8 pcs"),
            (self.B, "always discreet pants 8 pcs", "ALWAYS DISCREET لارج"),
        ):
            rows = client.get(
                f"/api/v1/products/?search={SHARED_BARCODE}"
            ).json()["results"]
            self.assertEqual([r["name"] for r in rows], [own])
            self.assertNotIn(other, str(rows))

    def test_staff_barcode_filter_is_isolated_both_directions(self):
        rows_a = self.A.get(
            f"/api/v1/products/?barcode={SHARED_BARCODE}"
        ).json()["results"]
        rows_b = self.B.get(
            f"/api/v1/products/?barcode={SHARED_BARCODE}"
        ).json()["results"]
        self.assertEqual([(r["name"], r["price"]) for r in rows_a],
                         [("ALWAYS DISCREET لارج", "25.00")])
        self.assertEqual([(r["name"], r["price"]) for r in rows_b],
                         [("always discreet pants 8 pcs", "39.00")])

    def test_staff_barcode_only_in_one_pharmacy_not_found_in_the_other(self):
        self.assertEqual(
            self.B.get(f"/api/v1/products/?barcode={ONLY_A_BARCODE}").json()["results"], []
        )
        self.assertEqual(
            self.A.get(f"/api/v1/products/?barcode={ONLY_B_BARCODE}").json()["results"], []
        )
        # …and each owner does find their own.
        self.assertEqual(
            len(self.A.get(f"/api/v1/products/?barcode={ONLY_A_BARCODE}").json()["results"]), 1
        )
        self.assertEqual(
            len(self.B.get(f"/api/v1/products/?barcode={ONLY_B_BARCODE}").json()["results"]), 1
        )

    def test_staff_detail_never_serves_shared_or_foreign_images(self):
        body = self.B.get(f"/api/v1/products/{self.med_b.pk}/").json()
        self.assertEqual(body["image"], "", "legacy shared photo must not leak")
        self.assertNotIn("shared-donated-by-a", str(body))
        self.assertNotIn("a-own", str(body))

    # -------------------------------------------------------------------- POS

    def test_pos_catalog_contains_only_own_rows(self):
        rows_a = self.A.get("/api/v1/products/pos_catalog/").json()["results"]
        rows_b = self.B.get("/api/v1/products/pos_catalog/").json()["results"]
        by_bc_a = {r["barcode"]: r for r in rows_a}
        by_bc_b = {r["barcode"]: r for r in rows_b}
        self.assertEqual(by_bc_a[SHARED_BARCODE]["name"], "ALWAYS DISCREET لارج")
        self.assertEqual(by_bc_b[SHARED_BARCODE]["name"], "always discreet pants 8 pcs")
        self.assertIn(ONLY_A_BARCODE, by_bc_a)
        self.assertNotIn(ONLY_A_BARCODE, by_bc_b)
        self.assertIn(ONLY_B_BARCODE, by_bc_b)
        self.assertNotIn(ONLY_B_BARCODE, by_bc_a)
        # cache warmed by A must never surface to B (and vice versa)
        self.assertNotIn("Only In A", str(rows_b))
        self.assertNotIn("Only In B", str(rows_a))

    # ----------------------------------------------------------------- public

    def _price(self, slug, barcode):
        return self.anon.get(
            f"/api/v1/public/price-check/?store={slug}&barcode={barcode}"
        ).json()

    def test_public_price_page_shared_barcode_fully_isolated(self):
        ra = self._price("ctr-a", SHARED_BARCODE)
        rb = self._price("ctr-b", SHARED_BARCODE)
        self.assertEqual(
            (ra["found"], ra["name"], ra["price"]),
            (True, "ALWAYS DISCREET لارج", "25.00"),
        )
        self.assertEqual(
            (rb["found"], rb["name"], rb["price"]),
            (True, "always discreet pants 8 pcs", "39.00"),
        )
        # A's own photo for A; NOTHING for B — not A's photo, not the legacy
        # shared CatalogItem photo (the pre-isolation leak).
        self.assertIn("a-own", ra["image"])
        self.assertEqual(rb["image"], "")

    def test_public_price_page_not_found_across_tenants(self):
        self.assertEqual(self._price("ctr-b", ONLY_A_BARCODE), {"found": False})
        self.assertEqual(self._price("ctr-a", ONLY_B_BARCODE), {"found": False})
        self.assertTrue(self._price("ctr-a", ONLY_A_BARCODE)["found"])
        self.assertTrue(self._price("ctr-b", ONLY_B_BARCODE)["found"])

    def test_public_search_is_isolated_both_directions(self):
        ra = self.anon.get(
            "/api/v1/public/price-check/?store=ctr-a&q=Only In"
        ).json()["results"]
        rb = self.anon.get(
            "/api/v1/public/price-check/?store=ctr-b&q=Only In"
        ).json()["results"]
        self.assertEqual([r["name"] for r in ra], ["Only In A"])
        self.assertEqual([r["name"] for r in rb], ["Only In B"])
        # search results never carry the legacy shared photo either
        rb2 = self.anon.get(
            "/api/v1/public/price-check/?store=ctr-b&q=discreet"
        ).json()["results"]
        self.assertEqual(len(rb2), 1)
        self.assertEqual(rb2[0]["image"], "")
        self.assertNotIn("shared-donated-by-a", str(rb2))

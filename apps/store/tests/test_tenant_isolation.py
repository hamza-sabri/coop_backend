"""Tenant isolation — the pillar of the app.

Two stores, two staff accounts, one account with no store. These
tests assert that NO endpoint, filter, write, reference, aggregate, cache
or public lookup can ever cross the tenant boundary. If any test here fails,
do not ship.

Run: python manage.py test apps.store
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()


class TenantFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cache.clear()
        cls.ph_a = models.Store.objects.create(name="صيدلية الرحمة", slug="test-a")
        cls.ph_b = models.Store.objects.create(name="صيدلية النور", slug="test-b")
        cls.user_a = User.objects.create_user("staff_a", password="x", store=cls.ph_a)
        cls.user_b = User.objects.create_user("staff_b", password="x", store=cls.ph_b)
        cls.user_none = User.objects.create_user("lost", password="x")

        cls.med_a = models.Product.objects.create(
            store=cls.ph_a, name="Med A", barcode="555", price=Decimal("10.00"), stock=5
        )
        cls.med_b = models.Product.objects.create(
            store=cls.ph_b, name="Med B", barcode="555", price=Decimal("99.00"), stock=5
        )
        cls.cust_a = models.Customer.objects.create(store=cls.ph_a, name="Ahmad", phone="0561")
        cls.cust_b = models.Customer.objects.create(store=cls.ph_b, name="Basel", phone="0562")

    def setUp(self):
        cache.clear()
        self.A = APIClient()
        self.A.force_authenticate(self.user_a)
        self.B = APIClient()
        self.B.force_authenticate(self.user_b)
        self.N = APIClient()
        self.N.force_authenticate(self.user_none)
        self.anon = APIClient()


class ReadScopingTests(TenantFixtureMixin, TestCase):
    LIST_URLS = [
        "/api/v1/products/",
        "/api/v1/customers/",
        "/api/v1/debts/",
        "/api/v1/sales/",
        "/api/v1/categories/",
        "/api/v1/manufacturers/",
    ]

    def test_lists_never_contain_other_tenants_rows(self):
        models.Debt.objects.create(store=self.ph_b, customer=self.cust_b)
        models.Sale.objects.create(store=self.ph_b, discounted_total=Decimal("9"))
        models.Category.objects.create(store=self.ph_b, name="مسكنات")
        models.Manufacturer.objects.create(store=self.ph_b, name="Bayer")
        for url in self.LIST_URLS:
            with self.subTest(url=url):
                results = self.A.get(url).json()["results"]
                ids = {r["id"] for r in results}
                names = {r.get("name") for r in results}
                self.assertNotIn("Med B", names)
                self.assertNotIn("Basel", names)
                # every listed row must belong to store A in the DB
                model = {
                    "/api/v1/products/": models.Product,
                    "/api/v1/customers/": models.Customer,
                    "/api/v1/debts/": models.Debt,
                    "/api/v1/sales/": models.Sale,
                    "/api/v1/categories/": models.Category,
                    "/api/v1/manufacturers/": models.Manufacturer,
                }[url]
                for pk in ids:
                    self.assertEqual(
                        model.objects.unscoped().get(pk=pk).store_id, self.ph_a.pk
                    )

    def test_detail_routes_404_across_tenants(self):
        debt_b = models.Debt.objects.create(store=self.ph_b, customer=self.cust_b)
        sale_b = models.Sale.objects.create(store=self.ph_b, discounted_total=Decimal("9"))
        for url in (
            f"/api/v1/products/{self.med_b.pk}/",
            f"/api/v1/customers/{self.cust_b.pk}/",
            f"/api/v1/debts/{debt_b.pk}/",
            f"/api/v1/sales/{sale_b.pk}/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.A.get(url).status_code, 404)
                # sales are immutable (405 by design); everything else 404
                self.assertIn(self.A.patch(url, {}, format="json").status_code, (404, 405))
                self.assertEqual(self.A.delete(url).status_code, 404)

    def test_filters_and_search_stay_inside_tenant(self):
        # same barcode exists in both stores at different prices
        rows = self.A.get("/api/v1/products/?barcode=555").json()["results"]
        self.assertEqual([r["name"] for r in rows], ["Med A"])
        rows = self.B.get("/api/v1/products/?barcode=555").json()["results"]
        self.assertEqual([r["name"] for r in rows], ["Med B"])
        rows = self.A.get("/api/v1/products/?search=Med").json()["results"]
        self.assertEqual({r["name"] for r in rows}, {"Med A"})


class WriteScopingTests(TenantFixtureMixin, TestCase):
    def test_creates_are_stamped_with_the_users_pharmacy(self):
        r = self.A.post("/api/v1/products/", {"name": "New", "price": "3"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(models.Product.objects.unscoped().get(pk=r.json()["id"]).store_id, self.ph_a.pk)
        r = self.A.post("/api/v1/customers/", {"name": "C"}, format="json")
        self.assertEqual(models.Customer.objects.unscoped().get(pk=r.json()["id"]).store_id, self.ph_a.pk)

    def test_cross_tenant_references_rejected(self):
        r = self.A.post("/api/v1/debts/", {"customer": self.cust_b.pk, "amount": "50"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.A.post(
            "/api/v1/sales/",
            {"payment_method": "cash", "items": [{"product": self.med_b.pk, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        r = self.A.post(
            "/api/v1/sales/",
            {"payment_method": "debt", "customer": self.cust_b.pk,
             "items": [{"product": self.med_a.pk, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        # debt item referencing the other tenant's med
        r = self.A.post(
            "/api/v1/debts/",
            {"customer": self.cust_a.pk, "items": [{"product": self.med_b.pk, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        # sale/debt items referencing the other tenant's VARIANT — its label
        # and price must never be snapshotted into this tenant's records
        variant_b = models.ProductVariant.objects.create(
            product=self.med_b, label="B-only", price=Decimal("77.00")
        )
        r = self.A.post(
            "/api/v1/sales/",
            {"payment_method": "cash",
             "items": [{"product": self.med_a.pk, "variant": variant_b.pk, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        r = self.A.post(
            "/api/v1/debts/",
            {"customer": self.cust_a.pk,
             "items": [{"product": self.med_a.pk, "variant": variant_b.pk, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertNotIn("B-only", r.content.decode())
        self.assertFalse(
            models.DebtItem.objects.unscoped().filter(variant=variant_b).exists()
        )

    def test_credit_sale_stamps_debt_with_same_pharmacy(self):
        r = self.A.post(
            "/api/v1/sales/",
            {"payment_method": "debt", "customer": self.cust_a.pk,
             "items": [{"product": self.med_a.pk, "quantity": 2}]},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        debt = models.Debt.objects.unscoped().get(pk=r.json()["debt"])
        self.assertEqual(debt.store_id, self.ph_a.pk)

    def test_phone_unique_per_pharmacy_only(self):
        r = self.B.post("/api/v1/customers/", {"name": "Same number", "phone": "0561"}, format="json")
        self.assertEqual(r.status_code, 201)  # A already has 0561 — B may too
        r = self.A.post("/api/v1/customers/", {"name": "Dup", "phone": "0561"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_taxonomy_same_name_two_rows(self):
        self.A.post("/api/v1/products/", {"name": "X1", "price": "1", "category": "مسكنات"}, format="json")
        self.B.post("/api/v1/products/", {"name": "X2", "price": "1", "category": "مسكنات"}, format="json")
        self.assertEqual(models.Category.objects.unscoped().filter(name="مسكنات").count(), 2)
        cats = self.A.get("/api/v1/categories/").json()["results"]
        self.assertEqual(len([c for c in cats if c["name"] == "مسكنات"]), 1)


class AggregateScopingTests(TenantFixtureMixin, TestCase):
    def test_stats_dashboards_and_quick_are_per_tenant_even_with_cache(self):
        self.A.post(
            "/api/v1/sales/",
            {"payment_method": "debt", "customer": self.cust_a.pk,
             "items": [{"product": self.med_a.pk, "quantity": 2}]},
            format="json",
        )
        # warm A's caches, then read as B — B must still see zeros
        self.assertEqual(self.A.get("/api/v1/sales/stats/").json()["periods"]["today"]["count"], 1)
        self.assertEqual(self.B.get("/api/v1/sales/stats/").json()["periods"]["today"]["count"], 0)
        self.assertGreater(Decimal(str(self.A.get("/api/v1/debts/dashboard/").json()["total_outstanding"])), 0)
        self.assertEqual(Decimal(str(self.B.get("/api/v1/debts/dashboard/").json()["total_outstanding"])), 0)
        self.assertEqual(self.B.get("/api/v1/customers/quick/").json()["count"], 1)
        names_b = {m["name"] for m in self.B.get("/api/v1/products/pos_catalog/").json()["results"]}
        self.assertEqual(names_b, {"Med B"})
        stats_b = self.B.get("/api/v1/products/stats/").json()
        self.assertEqual(stats_b["total_items"], 1)

    def test_settle_scoped(self):
        models.Debt.objects.create(
            store=self.ph_a, customer=self.cust_a, discounted_total=Decimal("30")
        )
        self.assertEqual(
            self.B.post(f"/api/v1/customers/{self.cust_a.pk}/settle/", {}, format="json").status_code,
            404,
        )


class PublicEndpointTests(TenantFixtureMixin, TestCase):
    def test_price_check_requires_slug_and_is_scoped(self):
        ra = self.anon.get("/api/v1/public/price-check/?store=test-a&barcode=555").json()
        rb = self.anon.get("/api/v1/public/price-check/?store=test-b&barcode=555").json()
        self.assertEqual((ra["found"], ra["name"], ra["price"]), (True, "Med A", "10.00"))
        self.assertEqual((rb["found"], rb["name"], rb["price"]), (True, "Med B", "99.00"))
        # No slug at all → the tenant-API guard rejects it outright (400).
        r = self.anon.get("/api/v1/public/price-check/?barcode=555")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"detail": "store_id is required"})
        # A present-but-unknown slug still answers not-found — never global.
        self.assertEqual(
            self.anon.get("/api/v1/public/price-check/?store=ghost&barcode=555").json(),
            {"found": False},
        )

    def test_price_check_never_leaks_internal_fields(self):
        r = self.anon.get("/api/v1/public/price-check/?store=test-a&barcode=555").json()
        self.assertEqual(set(r.keys()), {"found", "name", "price", "image"})
        for forbidden in ("cost", "stock", "available", "category", "id", "source_id", "notes"):
            self.assertNotIn(forbidden, r)

    def test_shared_catalog_presence_never_leaks_to_unlisting_pharmacy(self):
        """Store B lists a barcode that also has a LEGACY shared CatalogItem
        row. Store A does NOT list it — A's public page must answer exactly
        like the item doesn't exist. Customers never learn what OTHER
        stores stock or charge."""
        legacy = models.CatalogItem.objects.create(barcode="7771", name="Exclusive B")
        models.Product.objects.create(
            store=self.ph_b, name="Exclusive B", barcode="7771",
            price=Decimal("30.00"), stock=9, catalog_item=legacy,
        )
        rb = self.anon.get("/api/v1/public/price-check/?store=test-b&barcode=7771").json()
        self.assertTrue(rb["found"])
        ra = self.anon.get("/api/v1/public/price-check/?store=test-a&barcode=7771").json()
        self.assertEqual(ra, {"found": False})

    def test_unpriced_listing_answers_not_found(self):
        """A listing whose price was never set (0) must read as not-found
        publicly — never 'we have it, come ask'. Setting a price makes it
        appear; the shared catalog alone never does."""
        med = models.Product.objects.create(
            store=self.ph_a, name="No Price Yet", barcode="7772",
            price=Decimal("0.00"), stock=5,
        )
        r = self.anon.get("/api/v1/public/price-check/?store=test-a&barcode=7772").json()
        self.assertEqual(r, {"found": False})
        med.price = Decimal("8.50")
        med.save()
        r = self.anon.get("/api/v1/public/price-check/?store=test-a&barcode=7772").json()
        self.assertEqual((r["found"], r["price"]), (True, "8.50"))


class NoPharmacyAccountTests(TenantFixtureMixin, TestCase):
    URLS = [
        "/api/v1/products/",
        "/api/v1/customers/",
        "/api/v1/debts/",
        "/api/v1/sales/",
        "/api/v1/categories/",
        "/api/v1/manufacturers/",
        "/api/v1/debts/dashboard/",
        "/api/v1/products/stats/",
        "/api/v1/products/pos_catalog/",
        "/api/v1/sales/stats/",
        "/api/v1/customers/quick/",
    ]

    def test_account_without_pharmacy_gets_400_everywhere(self):
        """The tenant-API guard: no resolvable store → 400 with the exact
        machine-checkable message (was a 403 before the guard existed)."""
        for url in self.URLS:
            with self.subTest(url=url):
                r = self.N.get(url)
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json(), {"detail": "store_id is required"})
        r = self.N.post(
            "/api/v1/products/", {"name": "X", "price": "1"}, format="json"
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"detail": "store_id is required"})

    def test_anonymous_gets_401_on_staff_endpoints(self):
        for url in self.URLS:
            with self.subTest(url=url):
                self.assertEqual(self.anon.get(url).status_code, 401)


class GalleryIsolationTests(TenantFixtureMixin, TestCase):
    """Product image gallery lives OUTSIDE the obvious CRUD paths, so it
    gets its own isolation proof: one tenant can never read, reference, or
    delete another tenant's photos."""

    def test_remove_images_cannot_delete_another_tenants_photo(self):
        img_b = models.ProductImage.objects.create(
            product=self.med_b, image="https://cdn.example/b-secret.png", position=1
        )
        # A edits their OWN med and tries to remove B's image by id.
        r = self.A.patch(
            f"/api/v1/products/{self.med_a.pk}/",
            {"remove_images": str(img_b.pk)},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        # Ids from another row/tenant are inert — B's photo survives.
        self.assertTrue(models.ProductImage.objects.unscoped().filter(pk=img_b.pk).exists())

    def test_cannot_reach_another_tenants_gallery_directly(self):
        img_b = models.ProductImage.objects.create(
            product=self.med_b, image="https://cdn.example/b2.png", position=1
        )
        r = self.A.patch(
            f"/api/v1/products/{self.med_b.pk}/",
            {"remove_images": str(img_b.pk)},
            format="json",
        )
        self.assertEqual(r.status_code, 404)
        self.assertTrue(models.ProductImage.objects.unscoped().filter(pk=img_b.pk).exists())

    def test_detail_gallery_never_shows_other_tenants_images(self):
        models.ProductImage.objects.create(
            product=self.med_b, image="https://cdn.example/bb.png", position=1
        )
        body = self.A.get(f"/api/v1/products/{self.med_a.pk}/").json()
        self.assertEqual(body.get("images", []), [])


class PosCartIsolationTests(TenantFixtureMixin, TestCase):
    """The POS cart-state blob is stored per USER (one row each). Because a
    user belongs to exactly one store, a cart can never surface to another
    account — and therefore never to another tenant."""

    CART = "/api/v1/pos/cart-state/"

    def test_cart_is_per_account_and_never_crosses(self):
        saved = self.A.put(
            self.CART, {"data": {"carts": [{"id": "c1", "items": []}]}}, format="json"
        )
        self.assertEqual(saved.status_code, 200, saved.content)
        # B (other tenant) sees an empty cart — never A's.
        self.assertEqual(self.B.get(self.CART).json()["data"], {})
        # B saving their own must not disturb A's.
        self.B.put(self.CART, {"data": {"carts": [{"id": "b1"}]}}, format="json")
        self.assertEqual(
            self.A.get(self.CART).json()["data"], {"carts": [{"id": "c1", "items": []}]}
        )

    def test_cart_requires_authentication(self):
        self.assertEqual(self.anon.get(self.CART).status_code, 401)


class PublicImageScopingTests(TenantFixtureMixin, TestCase):
    """Same barcode in both stores -> the public price-check must return
    the image of the QUERIED store, never the other tenant's photo."""

    def test_public_image_belongs_to_the_queried_pharmacy(self):
        self.med_a.image = "https://cdn.example/a-only.png"
        self.med_a.save(update_fields=["image"])
        self.med_b.image = "https://cdn.example/b-only.png"
        self.med_b.save(update_fields=["image"])
        ra = self.anon.get(
            "/api/v1/public/price-check/?store=test-a&barcode=555"
        ).json()
        rb = self.anon.get(
            "/api/v1/public/price-check/?store=test-b&barcode=555"
        ).json()
        self.assertIn("a-only", ra["image"])
        self.assertIn("b-only", rb["image"])
        self.assertNotIn("b-only", ra["image"])


class PublicThrottleGuardTests(TestCase):
    """The unauthenticated price-check is the one endpoint the whole internet
    can hit. It MUST stay rate-limited. A per-tenant throttle is unnecessary:
    ScopedRateThrottle keys anonymous callers by IP and authenticated staff by
    user, so no store can exhaust another's quota. This guards the config
    from silent regressions."""

    def test_price_check_endpoint_stays_throttled(self):
        from rest_framework.throttling import ScopedRateThrottle

        from apps.store.views import PublicPriceCheckView

        self.assertIn(ScopedRateThrottle, PublicPriceCheckView.throttle_classes)
        self.assertEqual(PublicPriceCheckView.throttle_scope, "price_check")

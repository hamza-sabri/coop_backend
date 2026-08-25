"""The tenant-API 400 guard — permanent contract (plan §3.5 rule 3).

EVERY endpoint on the tenant API surface, called without a resolvable
store, must fail with HTTP 400 and the exact machine-checkable body:

    {"detail": "store_id is required"}

"Resolvable" means: the authenticated user's store (staff endpoints) or
the ?store= slug (anonymous public tenant endpoints). The choke point is
`apps.core.permissions.StoreResolved`, attached to every tenant view (the
scoped viewsets inherit it via StoreScopedMixin); `request_pharmacy_id()`
and serializers' `_pharmacy_id()` raise the same 400 as backstops.

DELIBERATE EXEMPTIONS (no store by design — asserted at the bottom so a
regression in either direction fails the build):
- /api/v1/auth/*            login/refresh/logout/me
- /healthz/                 DB-free liveness probe
- /admin/*                  Django admin (cross-tenant ops)
- /api/schema/, /api/docs/* OpenAPI + docs
- /api/v1/public/stats/     platform-wide marketing aggregates
- /api/v1/pos/cart-state/   per-USER resource (account-scoped, not tenant)

Run: python manage.py test apps.store.tests.test_pharmacy_required
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()

REQUIRED = {"detail": "store_id is required"}


class PharmacyRequiredContractTests(TestCase):
    """No resolvable store → 400 REQUIRED; with one → works as before."""

    # Staff endpoints (store resolves from the authenticated user).
    # (method, url, payload) — payloads are minimal but valid-shaped so the
    # guard, not payload validation, is provably what rejects the request.
    STAFF_ENDPOINTS = [
        ("get", "/api/v1/products/", None),
        ("get", "/api/v1/products/?search=panadol", None),      # med search
        ("get", "/api/v1/products/?barcode=6291041500213", None),  # scan lookup
        ("get", "/api/v1/products/pos_catalog/", None),         # POS catalog
        ("get", "/api/v1/products/stats/", None),
        ("get", "/api/v1/products/catalog_version/", None),
        ("get", "/api/v1/variants/", None),
        ("get", "/api/v1/categories/", None),
        ("get", "/api/v1/manufacturers/", None),
        ("get", "/api/v1/customers/", None),
        ("get", "/api/v1/customers/quick/", None),
        ("get", "/api/v1/debts/", None),
        ("get", "/api/v1/debts/dashboard/", None),
        ("get", "/api/v1/sales/", None),
        ("get", "/api/v1/sales/stats/", None),
        ("get", "/api/v1/reports/summary/", None),                 # reports
        ("get", "/api/v1/reports/teaser/", None),
        ("get", "/api/v1/reports/products/", None),
        ("get", "/api/v1/reports/top-products/", None),
        ("get", "/api/v1/reports/export/", None),
        ("get", "/api/v1/reports/sales/summary/", None),
        ("get", "/api/v1/reports/sales/export/", None),
        ("get", "/api/v1/qr/price-page/", None),
        ("get", "/api/v1/staff/", None),                            # user management
        ("post", "/api/v1/staff/", {"username": "x", "password": "xxxx"}),
        ("post", "/api/v1/products/", {"name": "X", "price": "1"}),
        ("post", "/api/v1/customers/", {"name": "C"}),
        ("post", "/api/v1/debts/", {"amount": "5"}),
        ("post", "/api/v1/sales/", {"payment_method": "cash", "items": []}),
        ("post", "/api/v1/products/bulk_delete/", {"all": True}),
        ("post", "/api/v1/sales/bulk_delete/", {"all": True}),
        ("get", "/api/v1/reports/restock-quota/", None),
        ("get", "/api/v1/purchase-orders/", None),
        ("post", "/api/v1/purchase-orders/", {"items": []}),
        ("post", "/api/v1/import/hesabate/products/", {}),         # import
    ]

    @classmethod
    def setUpTestData(cls):
        cls.store = models.Store.objects.create(name="صيدلية", slug="req-a")
        # Both users are OWNERS (the model default) so role/module gates pass
        # and the store guard is provably the thing that rejects.
        cls.user = User.objects.create_user("req_staff", password="x", store=cls.store)
        cls.user_no_pharmacy = User.objects.create_user("req_lost", password="x")
        cls.med = models.Product.objects.create(
            store=cls.store, name="Panadol", barcode="6291041500213",
            price=Decimal("10.00"), stock=Decimal("3"),
        )

    def setUp(self):
        cache.clear()
        self.WITH = APIClient()
        self.WITH.force_authenticate(self.user)
        self.WITHOUT = APIClient()
        self.WITHOUT.force_authenticate(self.user_no_pharmacy)
        self.anon = APIClient()

    def _call(self, client, method, url, payload):
        kwargs = {"format": "json"} if method == "post" else {}
        return getattr(client, method)(url, payload, **kwargs) if method == "post" \
            else getattr(client, method)(url)

    # ------------------------------------------------- staff surface: no tenant

    def test_every_staff_endpoint_400s_without_a_pharmacy(self):
        for method, url, payload in self.STAFF_ENDPOINTS:
            with self.subTest(method=method, url=url):
                r = self._call(self.WITHOUT, method, url, payload)
                self.assertEqual(r.status_code, 400, f"{url}: {r.content[:200]}")
                self.assertEqual(r.json(), REQUIRED, url)

    def test_staff_endpoints_work_with_a_pharmacy(self):
        """The same requests with a store never see the guard's answer."""
        cases = [
            ("/api/v1/products/", 1),
            ("/api/v1/products/?search=Panadol", 1),
            ("/api/v1/products/?barcode=6291041500213", 1),
            ("/api/v1/products/?barcode=0000000000000", 0),
        ]
        for url, n in cases:
            with self.subTest(url=url):
                r = self.WITH.get(url)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(len(r.json()["results"]), n)
        r = self.WITH.get("/api/v1/products/pos_catalog/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 1)
        for url in (
            "/api/v1/customers/", "/api/v1/debts/", "/api/v1/sales/",
            "/api/v1/sales/stats/", "/api/v1/debts/dashboard/",
            "/api/v1/reports/summary/", "/api/v1/reports/teaser/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.WITH.get(url).status_code, 200, url)
        # Import: past the guard, the (missing file) validation answers —
        # a DIFFERENT 400 proves the store guard let the request through.
        r = self.WITH.post("/api/v1/import/hesabate/products/", {}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertNotEqual(r.json(), REQUIRED)

    def test_anonymous_still_gets_401_not_400_on_staff_endpoints(self):
        """The guard must not swallow authentication: anonymous callers keep
        the 401 contract (only store-less AUTHENTICATED accounts get 400)."""
        for url in ("/api/v1/products/", "/api/v1/sales/", "/api/v1/reports/summary/"):
            with self.subTest(url=url):
                self.assertEqual(self.anon.get(url).status_code, 401)

    # ------------------------------------------- public tenant surface (slug)

    def test_public_price_check_requires_the_slug(self):
        for qs in ("", "?barcode=6291041500213", "?q=panadol", "?store=", "?store=%20"):
            with self.subTest(qs=qs):
                r = self.anon.get(f"/api/v1/public/price-check/{qs}")
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json(), REQUIRED)

    def test_public_price_check_works_with_the_slug(self):
        r = self.anon.get(
            "/api/v1/public/price-check/?store=req-a&barcode=6291041500213"
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["found"], body["name"], body["price"]),
                         (True, "Panadol", "10.00"))
        r = self.anon.get("/api/v1/public/price-check/?store=req-a&q=Pan")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([m["name"] for m in r.json()["results"]], ["Panadol"])
        # unknown slug: resolvable input, empty answer — NOT a 400, NOT data
        r = self.anon.get("/api/v1/public/price-check/?store=ghost&barcode=1")
        self.assertEqual((r.status_code, r.json()), (200, {"found": False}))

    def test_public_branding_requires_the_slug(self):
        for url in ("/api/v1/public/branding/", "/api/v1/public/branding/icon/"):
            with self.subTest(url=url):
                r = self.anon.get(url)
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json(), REQUIRED)
        # with the slug: same behavior as before (200 / 404-no-logo)
        r = self.anon.get("/api/v1/public/branding/?store=req-a")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "صيدلية")
        r = self.anon.get("/api/v1/public/branding/icon/?store=req-a")
        self.assertEqual(r.status_code, 404)  # tenant has no logo — unchanged

    # ------------------------------------------------- deliberate exemptions

    def test_exempt_endpoints_still_work_without_any_pharmacy(self):
        # liveness probe
        self.assertEqual(self.anon.get("/healthz/").status_code, 200)
        # auth surface: a store-less account can still log in and see /me/
        r = self.anon.post(
            "/api/v1/auth/login/",
            {"username": "req_lost", "password": "x"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        r = self.WITHOUT.get("/api/v1/auth/me/")
        self.assertEqual(r.status_code, 200)
        # central marketing stats: platform-wide by design
        self.assertEqual(self.anon.get("/api/v1/public/stats/").status_code, 200)
        # POS cart state: per-USER resource, tenant-free by design
        r = self.WITHOUT.get("/api/v1/pos/cart-state/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"], {})

"""Roles, per-store usernames, plans, and the paid reports module.

Run: python manage.py test apps.store.tests.test_roles_plans_reports
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models
from apps.store.modules import pharmacy_modules

User = get_user_model()


def make_user(store, username, role="owner", password="pw-123456"):
    u = User.objects.create_user(
        username=username, password=password, store=store, role=role
    )
    return u


class PerPharmacyUsernameAuthTests(TestCase):
    """Usernames repeat across stores; login is scoped by slug."""

    def setUp(self):
        cache.clear()
        self.p1 = models.Store.objects.create(name="P1", slug="rp-one")
        self.p2 = models.Store.objects.create(name="P2", slug="rp-two")
        self.u1 = make_user(self.p1, "sara", password="password-one")
        self.u2 = make_user(self.p2, "sara", password="password-two")

    def _login(self, **payload):
        return APIClient().post("/api/v1/auth/login/", payload, format="json")

    def test_same_username_in_two_pharmacies_allowed(self):
        self.assertEqual(User.objects.filter(username="sara").count(), 2)

    def test_login_scoped_by_pharmacy_slug(self):
        r = self._login(username="sara", password="password-two", store="rp-two")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["store_slug"], "rp-two")
        # Right password, WRONG store -> refused.
        r = self._login(username="sara", password="password-two", store="rp-one")
        self.assertEqual(r.status_code, 401)

    def test_central_login_resolved_by_password(self):
        r = self._login(username="sara", password="password-one")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["store_slug"], "rp-one")

    def test_central_login_ambiguous_password_refused(self):
        # Same username AND same password in two stores: without a slug
        # there is no safe answer — refuse instead of guessing a tenant.
        self.u2.set_password("password-one")
        self.u2.save()
        r = self._login(username="sara", password="password-one")
        self.assertEqual(r.status_code, 401)
        r = self._login(username="sara", password="password-one", store="rp-two")
        self.assertEqual(r.status_code, 200)

    def test_role_in_me_payload(self):
        r = self._login(username="sara", password="password-one", store="rp-one")
        self.assertEqual(r.json()["user"]["role"], "owner")


class PlanModulesTests(TestCase):
    def test_no_plan_legacy_semantics(self):
        p = models.Store.objects.create(name="L", slug="pl-legacy")
        self.assertIn("pos", pharmacy_modules(p))  # empty list = everything
        p.enabled_modules = ["pos"]
        self.assertEqual(pharmacy_modules(p), frozenset({"pos"}))

    def test_plan_union_extras(self):
        plan = models.Plan.objects.create(name="أساسي", modules=["pos", "inventory"])
        p = models.Store.objects.create(
            name="B", slug="pl-basic", plan=plan, enabled_modules=["reports"]
        )
        self.assertEqual(
            pharmacy_modules(p), frozenset({"pos", "inventory", "reports"})
        )
        # An empty extras list with a plan means JUST the plan — no legacy
        # "empty = everything" surprise.
        p.enabled_modules = []
        self.assertEqual(pharmacy_modules(p), frozenset({"pos", "inventory"}))

    def test_inactive_plan_falls_back_to_legacy(self):
        plan = models.Plan.objects.create(name="X", modules=["pos"], is_active=False)
        p = models.Store.objects.create(name="C", slug="pl-off", plan=plan)
        self.assertEqual(pharmacy_modules(p), frozenset(pharmacy_modules(p)))
        self.assertIn("reports", pharmacy_modules(p))  # legacy: empty = all


class ReportsAccessAndDataTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = models.Store.objects.create(name="R", slug="rr-main")
        self.other = models.Store.objects.create(name="O", slug="rr-other")
        self.owner = make_user(self.store, "boss", role="owner")
        self.employee = make_user(self.store, "clerk", role="employee")

        m = models.Product.objects
        self.zero = m.create(store=self.store, name="بدون سعر", price=0, stock=3)
        self.below = m.create(
            store=self.store, name="خاسر", price=Decimal("5"),
            cost=Decimal("9"), stock=2,
        )
        self.negative = m.create(
            store=self.store, name="سالب", price=Decimal("4"), stock=Decimal("-1")
        )
        self.ok = m.create(
            store=self.store, name="سليم", price=Decimal("10"),
            cost=Decimal("6"), stock=8, barcode="629000000001",
        )
        # Other tenant's junk must NEVER appear in this store's reports.
        m.create(store=self.other, name="غريب", price=0, stock=-5)

        sale = models.Sale.objects.create(store=self.store)
        models.SaleItem.objects.create(
            sale=sale, product=self.ok, medication_name="سليم",
            unit_price=Decimal("10"), quantity=Decimal("7"),
        )
        sale.recalculate_total()

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def test_employee_blocked_owner_allowed(self):
        for path in (
            "/api/v1/reports/summary/",
            "/api/v1/reports/products/",
            "/api/v1/reports/top-products/",
            "/api/v1/reports/export/",
            "/api/v1/qr/price-page/",
        ):
            self.assertEqual(
                self._client(self.employee).get(path).status_code, 403, path
            )
            self.assertEqual(self._client(self.owner).get(path).status_code, 200, path)

    def test_reports_module_gate(self):
        # Store on a plan WITHOUT reports -> even the owner is locked out.
        plan = models.Plan.objects.create(name="بلا تقارير", modules=["pos"])
        self.store.plan = plan
        self.store.save()
        r = self._client(self.owner).get("/api/v1/reports/summary/")
        self.assertEqual(r.status_code, 403)

    def test_summary_counts_are_tenant_scoped(self):
        data = self._client(self.owner).get("/api/v1/reports/summary/").json()
        self.assertEqual(data["issues"]["zero_price"], 1)
        self.assertEqual(data["issues"]["below_cost"], 1)
        self.assertEqual(data["issues"]["negative_stock"], 1)
        self.assertEqual(data["valuation"]["total_medications"], 4)
        self.assertEqual(data["sales"]["top_products"][0]["name"], "سليم")

    def test_summary_self_checks_pass_and_buckets_partition(self):
        """The summary carries machine-verified invariants — they must hold on
        real data, and out_of_stock/negative/in-stock must partition the
        catalogue exactly."""
        models.Product.objects.create(
            store=self.store, name="نافذ", price=Decimal("3"), stock=0
        )
        data = self._client(self.owner).get("/api/v1/reports/summary/").json()
        i, v = data["issues"], data["valuation"]
        self.assertTrue(data["checks"]["passed"], data["checks"]["details"])
        self.assertEqual(
            i["out_of_stock"] + i["negative_stock"] + v["in_stock"],
            v["total_medications"],
        )
        self.assertEqual(i["out_of_stock"], 1)
        self.assertLessEqual(i["low_stock"], v["in_stock"])
        self.assertEqual(data["meta"]["dead_days"], data["meta"]["days"])

    def test_dead_stock_equals_stocked_minus_sold_in_period(self):
        """The user-facing promise: راكد = everything stocked that did NOT
        sell within the selected period. With one stocked med sold (سليم),
        dead stock = in_stock − 1 — and the self-check agrees."""
        data = self._client(self.owner).get(
            "/api/v1/reports/summary/", {"days": 30}
        ).json()
        in_stock = data["valuation"]["in_stock"]
        self.assertEqual(data["issues"]["dead_stock"], in_stock - 1)
        dead = next(
            d for d in data["checks"]["details"] if d["name"] == "dead_stock"
        )
        self.assertTrue(dead["ok"])

    def test_products_filter_and_pagination(self):
        r = self._client(self.owner).get(
            "/api/v1/reports/products/", {"issue": "below_cost"}
        )
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["name"], "خاسر")
        r = self._client(self.owner).get(
            "/api/v1/reports/products/", {"issue": "nope"}
        )
        self.assertEqual(r.status_code, 400)

    def test_returns_count_negative_in_top_products(self):
        ret = models.Sale.objects.create(store=self.store, is_return=True)
        models.SaleItem.objects.create(
            sale=ret, product=self.ok, medication_name="سليم",
            unit_price=Decimal("10"), quantity=Decimal("2"),
        )
        rows = (
            self._client(self.owner)
            .get("/api/v1/reports/top-products/")
            .json()["results"]
        )
        top = next(r for r in rows if r["name"] == "سليم")
        self.assertEqual(Decimal(top["quantity"]), Decimal("5"))  # 7 - 2

    def test_export_returns_xlsx(self):
        r = self._client(self.owner).get(
            "/api/v1/reports/export/", {"report": "issues", "issue": "zero_price"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])

    def test_qr_is_png(self):
        r = self._client(self.owner).get("/api/v1/qr/price-page/")
        self.assertEqual(r["Content-Type"], "image/png")
        self.assertTrue(r.content.startswith(b"\x89PNG"))

    def test_new_data_hygiene_filters(self):
        m = models.Product.objects
        m.create(store=self.store, name="1234", price=Decimal("2"), barcode="abc")
        m.create(store=self.store, name=" ", price=Decimal("2"))
        m.create(
            store=self.store, name="طويل", price=Decimal("2"),
            barcode="1234567890123456",  # 16 chars
        )
        c = self._client(self.owner)

        def count(issue, **params):
            return c.get(
                "/api/v1/reports/products/", {"issue": issue, **params}
            ).json()["count"]

        self.assertEqual(count("name_no_letters"), 1)  # "1234"
        self.assertEqual(count("no_name"), 1)  # whitespace name
        # broken_barcode umbrella, aligned with the SCANNER's accept rule
        # (digits, 4–20 chars): "abc" is short AND non-numeric; the 16-digit
        # code is scannable and therefore HEALTHY now; empties are broken.
        healthy = models.Product.objects.unscoped().filter(
            store=self.store,
            barcode__in=["629000000001", "1234567890123456"],
        ).count()
        self.assertEqual(healthy, 2)
        total = models.Product.objects.unscoped().filter(store=self.store).count()
        self.assertEqual(count("broken_barcode"), total - healthy)
        # below_cost: price 5 / cost 9 exists; equal-price row shouldn't count
        # unless include_equal=1.
        m.create(
            store=self.store, name="متعادل", price=Decimal("7"),
            cost=Decimal("7"), stock=1,
        )
        self.assertEqual(count("below_cost"), 1)
        self.assertEqual(count("below_cost", include_equal="1"), 2)
        # low_stock respects the owner-controlled N — and excludes zero-stock
        # (those belong to out_of_stock; the buckets are disjoint).
        self.assertEqual(
            count("low_stock", low_stock_threshold="2"),
            models.Product.objects.unscoped().filter(
                store=self.store, stock__gt=0, stock__lte=2
            ).count(),
        )

    def test_all_scope_with_advanced_ranges(self):
        c = self._client(self.owner)
        body = c.get(
            "/api/v1/reports/products/",
            {"issue": "all", "price_min": "4.5", "price_max": "6"},
        ).json()
        self.assertEqual(body["count"], 1)  # only "خاسر" at price 5
        self.assertEqual(body["results"][0]["name"], "خاسر")
        body = c.get(
            "/api/v1/reports/products/", {"issue": "all", "stock_max": "-1"}
        ).json()
        self.assertEqual(body["count"], 1)  # negative stock row

    def test_teaser_open_to_employees_and_real(self):
        r = self._client(self.employee).get("/api/v1/reports/teaser/")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["zero_price"], 1)
        self.assertEqual(data["below_cost"], 1)
        self.assertEqual(data["negative_stock"], 1)
        self.assertEqual(data["top_product"], "سليم")

    def test_sales_reports_separate_module(self):
        # Legacy tenant (no plan): all modules -> owner gets sales analytics.
        r = self._client(self.owner).get("/api/v1/reports/sales/summary/")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for key in ("revenue", "avg_basket", "by_hour", "by_employee", "top_customers"):
            self.assertIn(key, data)
        # Employee: never.
        self.assertEqual(
            self._client(self.employee).get("/api/v1/reports/sales/summary/").status_code,
            403,
        )
        # Plan with inventory reports but WITHOUT sales_reports -> 403 on
        # sales analytics while /reports/summary/ keeps working.
        plan = models.Plan.objects.create(name="مخزون فقط", modules=["pos", "reports"])
        self.store.plan = plan
        self.store.save()
        from django.core.cache import cache as djcache

        djcache.clear()
        self.assertEqual(
            self._client(self.owner).get("/api/v1/reports/summary/").status_code, 200
        )
        self.assertEqual(
            self._client(self.owner).get("/api/v1/reports/sales/summary/").status_code,
            403,
        )

    def test_summary_includes_categories(self):
        from django.core.cache import cache as djcache

        djcache.clear()
        data = self._client(self.owner).get("/api/v1/reports/summary/").json()
        self.assertIn("categories", data)
        names = [c["name"] for c in data["categories"]]
        self.assertIn("بلا تصنيف", names)

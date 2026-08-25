"""Feature-module gating — the SaaS tier boundary.

A store subscribes to modules (`Store.enabled_modules`) and an owner
can further restrict each staff account (`User.allowed_modules`). These
tests assert the gate holds at both levels, that empty lists mean "no
restriction" (legacy tenants unaffected), and that the public price-check
disappears for tenants who didn't buy it.

Run: python manage.py test apps.store
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models
from apps.store.modules import ALL_MODULES, MODULES, effective_modules

User = get_user_model()


class ModuleFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cache.clear()
        # POS-only tenant, debts-only tenant, and a legacy tenant (empty list).
        cls.ph_pos = models.Store.objects.create(
            name="POS Only", slug="mod-pos", enabled_modules=["pos", "inventory"]
        )
        cls.ph_debts = models.Store.objects.create(
            name="Books Only", slug="mod-debts", enabled_modules=["debts", "customers"]
        )
        cls.ph_full = models.Store.objects.create(name="Legacy Full", slug="mod-full")

        cls.user_pos = User.objects.create_user("u_pos", password="x", store=cls.ph_pos)
        cls.user_debts = User.objects.create_user("u_debts", password="x", store=cls.ph_debts)
        cls.user_full = User.objects.create_user("u_full", password="x", store=cls.ph_full)
        # Full store, but this cashier is personally limited to POS.
        cls.user_cashier = User.objects.create_user(
            "u_cashier", password="x", store=cls.ph_full, allowed_modules=["pos"]
        )

        cls.med_pos = models.Product.objects.create(
            store=cls.ph_pos, name="Med P", barcode="777", price=Decimal("5.00"), stock=3
        )
        cls.med_full = models.Product.objects.create(
            store=cls.ph_full, name="Med F", barcode="888", price=Decimal("7.00"), stock=3
        )
        cls.cust_debts = models.Customer.objects.create(
            store=cls.ph_debts, name="Dana", phone="0590"
        )

    def setUp(self):
        cache.clear()
        self.POS = APIClient()
        self.POS.force_authenticate(self.user_pos)
        self.DEBTS = APIClient()
        self.DEBTS.force_authenticate(self.user_debts)
        self.FULL = APIClient()
        self.FULL.force_authenticate(self.user_full)
        self.CASHIER = APIClient()
        self.CASHIER.force_authenticate(self.user_cashier)
        self.anon = APIClient()


class EffectiveModulesTests(ModuleFixtureMixin, TestCase):
    def test_empty_lists_mean_everything(self):
        self.assertEqual(effective_modules(self.user_full), ALL_MODULES)

    def test_pharmacy_list_limits(self):
        self.assertEqual(effective_modules(self.user_pos), frozenset({"pos", "inventory"}))

    def test_user_list_intersects_with_pharmacy(self):
        self.assertEqual(effective_modules(self.user_cashier), frozenset({"pos"}))

    def test_user_grant_cannot_exceed_pharmacy(self):
        stray = User.objects.create_user(
            "stray", password="x", store=self.ph_pos, allowed_modules=["debts", "pos"]
        )
        self.assertEqual(effective_modules(stray), frozenset({"pos"}))

    def test_unknown_keys_ignored(self):
        ph = models.Store.objects.create(
            name="Odd", slug="mod-odd", enabled_modules=["pos", "hovercraft"]
        )
        u = User.objects.create_user("odd", password="x", store=ph)
        self.assertEqual(effective_modules(u), frozenset({"pos"}))

    def test_no_pharmacy_no_modules(self):
        lost = User.objects.create_user("lost2", password="x")
        self.assertEqual(effective_modules(lost), frozenset())


class PharmacyLevelGatingTests(ModuleFixtureMixin, TestCase):
    def test_pos_tenant_blocked_from_debts(self):
        for url in ["/api/v1/debts/", "/api/v1/debts/dashboard/"]:
            res = self.POS.get(url)
            self.assertEqual(res.status_code, 403, url)

    def test_pos_tenant_keeps_customers_for_credit_sales(self):
        # Customer profiles are shared plumbing — reachable with any of
        # (customers, debts, pos), so a POS tenant can still attach a sale
        # to a customer.
        self.assertEqual(self.POS.get("/api/v1/customers/").status_code, 200)

    def test_pos_tenant_keeps_pos_and_inventory(self):
        self.assertEqual(self.POS.get("/api/v1/sales/").status_code, 200)
        self.assertEqual(self.POS.get("/api/v1/products/").status_code, 200)
        self.assertEqual(self.POS.get("/api/v1/products/pos_catalog/").status_code, 200)

    def test_debts_tenant_blocked_from_pos_and_inventory(self):
        for url in [
            "/api/v1/sales/",
            "/api/v1/products/",
            "/api/v1/products/pos_catalog/",
            "/api/v1/categories/",
            "/api/v1/manufacturers/",
        ]:
            res = self.DEBTS.get(url)
            self.assertEqual(res.status_code, 403, url)

    def test_debts_tenant_keeps_debts_and_customers(self):
        self.assertEqual(self.DEBTS.get("/api/v1/debts/").status_code, 200)
        self.assertEqual(self.DEBTS.get("/api/v1/customers/").status_code, 200)

    def test_writes_blocked_too(self):
        res = self.POS.post("/api/v1/debts/", {"customer": self.cust_debts.pk, "items": []})
        self.assertEqual(res.status_code, 403)
        res = self.DEBTS.post(
            "/api/v1/products/",
            {"name": "M", "barcode": "999", "price": "1.00", "stock": 1},
        )
        self.assertEqual(res.status_code, 403)

    def test_imports_gated(self):
        res = self.POS.post("/api/v1/import/hesabate/products/")
        self.assertEqual(res.status_code, 403)

    def test_legacy_empty_list_gets_everything(self):
        for url in [
            "/api/v1/products/",
            "/api/v1/customers/",
            "/api/v1/debts/",
            "/api/v1/sales/",
        ]:
            self.assertEqual(self.FULL.get(url).status_code, 200, url)


class UserLevelGatingTests(ModuleFixtureMixin, TestCase):
    def test_cashier_gets_pos_only(self):
        self.assertEqual(self.CASHIER.get("/api/v1/sales/").status_code, 200)
        self.assertEqual(
            self.CASHIER.get("/api/v1/products/pos_catalog/").status_code, 200
        )
        self.assertEqual(self.CASHIER.get("/api/v1/products/").status_code, 403)
        self.assertEqual(self.CASHIER.get("/api/v1/debts/").status_code, 403)
        # Customer profiles are shared plumbing for POS credit sales.
        self.assertEqual(self.CASHIER.get("/api/v1/customers/").status_code, 200)
        # …but settling debts is a debts-module action.
        res = self.CASHIER.post(
            f"/api/v1/customers/{self.med_full.pk}/settle/", {}
        )
        self.assertEqual(res.status_code, 403)


class PublicPriceCheckGatingTests(ModuleFixtureMixin, TestCase):
    def _lookup(self, slug, barcode):
        return self.anon.get(
            f"/api/v1/public/price-check/?store={slug}&barcode={barcode}"
        )

    def test_tenant_without_module_returns_nothing(self):
        # ph_pos has ["pos", "inventory"] — no price_check.
        res = self._lookup("mod-pos", "777")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["found"])

    def test_legacy_tenant_still_answers(self):
        res = self._lookup("mod-full", "888")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["found"])

    def test_tenant_with_module_answers(self):
        ph = models.Store.objects.create(
            name="Scanner", slug="mod-scan", enabled_modules=["price_check"]
        )
        models.Product.objects.create(
            store=ph, name="Med S", barcode="123", price=Decimal("2.00"), stock=1
        )
        res = self._lookup("mod-scan", "123")
        self.assertTrue(res.json()["found"])


class MeEndpointModulesTests(ModuleFixtureMixin, TestCase):
    def test_me_reports_effective_modules(self):
        res = self.CASHIER.get("/api/v1/auth/me/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["modules"], ["pos"])

    def test_me_reports_all_for_legacy(self):
        res = self.FULL.get("/api/v1/auth/me/")
        self.assertEqual(sorted(res.json()["modules"]), sorted(MODULES))

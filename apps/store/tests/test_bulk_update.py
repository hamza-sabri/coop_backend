"""Bulk edit — the "fix imported data" tool.

This endpoint REWRITES many products at once, so it is covered hard:
tenant isolation, owner-only, targeting by filter, and the
price-from-cost-margin repair used on zero-priced imports.

Run: python manage.py test apps.store.tests.test_bulk_update
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()
URL = "/api/v1/products/bulk_update/"


def med(ph, name, **kw):
    kw.setdefault("price", Decimal("0"))
    kw.setdefault("cost", Decimal("0"))
    kw.setdefault("stock", Decimal("5"))
    return models.Product.objects.create(store=ph, name=name, **kw)


class BulkUpdateTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ph = models.Store.objects.create(name="A", slug="bu-a")
        self.other = models.Store.objects.create(name="B", slug="bu-b")
        self.owner = User.objects.create_user(
            username="owner", password="pw-123456", store=self.ph, role="owner"
        )
        self.employee = User.objects.create_user(
            username="emp", password="pw-123456", store=self.ph, role="employee"
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

        # zero-priced but with a real cost → the classic bad import
        self.a = med(self.ph, "A", price=Decimal("0"), cost=Decimal("10"))
        self.b = med(self.ph, "B", price=Decimal("0"), cost=Decimal("8"))
        # zero price AND no cost → can't derive a price
        self.c = med(self.ph, "C", price=Decimal("0"), cost=Decimal("0"))
        # a healthy product that must NOT be touched
        self.ok = med(self.ph, "OK", price=Decimal("20"), cost=Decimal("12"))
        # another tenant's zero-priced product — must never be touched
        self.foreign = med(self.other, "FOREIGN", price=Decimal("0"), cost=Decimal("9"))

    def test_price_from_cost_margin_fixes_only_matching_rows(self):
        r = self.api.post(
            URL,
            {
                "issue": "zero_price",
                "all_matching": True,
                "changes": {"price_from_cost_margin": 25},
            },
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["updated"], 2)  # a & b (c has no cost)

        self.a.refresh_from_db(); self.b.refresh_from_db()
        self.c.refresh_from_db(); self.ok.refresh_from_db()
        self.assertEqual(self.a.price, Decimal("12.50"))   # 10 × 1.25
        self.assertEqual(self.b.price, Decimal("10.00"))   # 8 × 1.25
        self.assertEqual(self.c.price, Decimal("0"))       # untouched (no cost)
        self.assertEqual(self.ok.price, Decimal("20"))     # healthy row untouched

    def test_never_touches_another_pharmacy(self):
        self.api.post(
            URL,
            {"issue": "zero_price", "all_matching": True,
             "changes": {"price_from_cost_margin": 50}},
            format="json",
        )
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.price, Decimal("0"))

    def test_explicit_ids_and_field_changes(self):
        r = self.api.post(
            URL,
            {"ids": [self.c.id], "changes": {"price": "7.50", "category": "مسكنات"}},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["updated"], 1)
        self.c.refresh_from_db()
        self.assertEqual(self.c.price, Decimal("7.50"))
        self.assertEqual(self.c.category.name, "مسكنات")
        # the category was created inside THIS store only
        self.assertEqual(self.c.category.store_id, self.ph.id)

    def test_employee_is_forbidden(self):
        api = APIClient()
        api.force_authenticate(self.employee)
        r = api.post(
            URL, {"ids": [self.a.id], "changes": {"price": "1"}}, format="json"
        )
        self.assertEqual(r.status_code, 403)
        self.a.refresh_from_db()
        self.assertEqual(self.a.price, Decimal("0"))

    def test_reported_count_is_honest(self):
        """The count must match what ACTUALLY changed. With a margin repair plus
        another change, rows without a cost keep their price but still receive
        the other change — they are modified, so they must be counted."""
        r = self.api.post(
            URL,
            {
                "ids": [self.a.id, self.c.id],
                "changes": {"price_from_cost_margin": 25, "category": "مراجعة"},
            },
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["updated"], 2)           # both rows changed
        self.assertEqual(body["priced_from_cost"], 1)  # only one had a cost
        self.c.refresh_from_db()
        self.assertEqual(self.c.category.name, "مراجعة")

    def test_annotated_and_subquery_filters_survive_bulk_update(self):
        """name_length uses an annotation; dead_stock / duplicate_barcode use
        subqueries. All must still work as bulk-update targets."""
        for issue in ("name_length", "dead_stock", "duplicate_barcode"):
            res = self.api.post(
                URL,
                {"issue": issue, "all_matching": True, "changes": {"cost": "1.00"}},
                format="json",
            )
            self.assertEqual(res.status_code, 200, f"{issue}: {res.content}")

    def test_margin_rounds_to_two_decimals(self):
        """3.33 × 1.25 = 4.1625 → must fit a 2dp field without erroring."""
        m = med(self.ph, "round", cost=Decimal("3.33"))
        r = self.api.post(
            URL,
            {"ids": [m.id], "changes": {"price_from_cost_margin": 25}},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        m.refresh_from_db()
        self.assertEqual(m.price, Decimal("4.16"))

    def test_guards(self):
        # no changes
        self.assertEqual(
            self.api.post(URL, {"ids": [self.a.id], "changes": {}}, format="json").status_code,
            400,
        )
        # no target
        self.assertEqual(
            self.api.post(URL, {"changes": {"price": "1"}}, format="json").status_code, 400
        )
        # negative money refused
        self.assertEqual(
            self.api.post(
                URL, {"ids": [self.a.id], "changes": {"price": "-5"}}, format="json"
            ).status_code,
            400,
        )

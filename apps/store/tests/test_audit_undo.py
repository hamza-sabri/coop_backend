"""Audit trail + undo for destructive bulk actions.

A bulk edit can rewrite thousands of prices; the store must be able to see
what happened and put it back. These tests prove the undo really restores the
previous values (not just flips a flag).

Run: python manage.py test apps.store.tests.test_audit_undo
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()
BULK = "/api/v1/products/bulk_update/"
AUDIT = "/api/v1/audit/"


class AuditUndoTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ph = models.Store.objects.create(name="A", slug="au-a")
        self.other = models.Store.objects.create(name="B", slug="au-b")
        self.owner = User.objects.create_user(
            username="owner", password="pw-123456", store=self.ph, role="owner"
        )
        self.employee = User.objects.create_user(
            username="emp", password="pw-123456", store=self.ph, role="employee"
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.a = models.Product.objects.create(
            store=self.ph, name="A", price=Decimal("0"),
            cost=Decimal("10"), stock=Decimal("5"),
        )
        self.b = models.Product.objects.create(
            store=self.ph, name="B", price=Decimal("0"),
            cost=Decimal("8"), stock=Decimal("5"),
        )

    def _bulk_margin(self):
        return self.api.post(
            BULK,
            {"issue": "zero_price", "all_matching": True,
             "changes": {"price_from_cost_margin": 25}},
            format="json",
        )

    def test_bulk_edit_is_logged_and_undo_restores_exact_values(self):
        r = self._bulk_margin()
        self.assertEqual(r.status_code, 200, r.content)
        audit_id = r.json()["audit_id"]
        self.assertTrue(r.json()["can_undo"])

        self.a.refresh_from_db(); self.b.refresh_from_db()
        self.assertEqual(self.a.price, Decimal("12.50"))
        self.assertEqual(self.b.price, Decimal("10.00"))

        # It shows up in the audit feed.
        feed = self.api.get(AUDIT).json()["results"]
        self.assertEqual(feed[0]["id"], audit_id)
        self.assertEqual(feed[0]["affected"], 2)
        self.assertEqual(feed[0]["actor"], "owner")

        # Undo puts the ORIGINAL prices back.
        u = self.api.post(f"{AUDIT}{audit_id}/undo/", {}, format="json")
        self.assertEqual(u.status_code, 200, u.content)
        self.assertEqual(u.json()["restored"], 2)
        self.a.refresh_from_db(); self.b.refresh_from_db()
        self.assertEqual(self.a.price, Decimal("0.00"))
        self.assertEqual(self.b.price, Decimal("0.00"))

    def test_undo_is_single_use(self):
        audit_id = self._bulk_margin().json()["audit_id"]
        self.assertEqual(
            self.api.post(f"{AUDIT}{audit_id}/undo/", {}, format="json").status_code, 200
        )
        again = self.api.post(f"{AUDIT}{audit_id}/undo/", {}, format="json")
        self.assertEqual(again.status_code, 400)

    def test_audit_is_tenant_scoped(self):
        self._bulk_margin()
        stranger = User.objects.create_user(
            username="other-owner", password="pw-123456",
            store=self.other, role="owner",
        )
        api = APIClient()
        api.force_authenticate(stranger)
        self.assertEqual(api.get(AUDIT).json()["results"], [])

    def test_employees_cannot_read_or_undo(self):
        audit_id = self._bulk_margin().json()["audit_id"]
        api = APIClient()
        api.force_authenticate(self.employee)
        self.assertEqual(api.get(AUDIT).status_code, 403)
        self.assertEqual(
            api.post(f"{AUDIT}{audit_id}/undo/", {}, format="json").status_code, 403
        )

    def test_category_change_is_undone_too(self):
        r = self.api.post(
            BULK,
            {"ids": [self.a.id], "changes": {"category": "مسكنات"}},
            format="json",
        )
        audit_id = r.json()["audit_id"]
        self.a.refresh_from_db()
        self.assertEqual(self.a.category.name, "مسكنات")

        self.api.post(f"{AUDIT}{audit_id}/undo/", {}, format="json")
        self.a.refresh_from_db()
        self.assertIsNone(self.a.category)  # back to no category

"""Offline-sync idempotency for non-sale creates (customer / debt / product).

A record created while offline is queued client-side with a per-store
`client_uuid` and re-sent on reconnect. A retry that reaches the server must NOT
create a duplicate — it returns the original row. Mirrors the Sale guarantee.

Run: python manage.py test apps.store.tests.test_offline_idempotency
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models
from apps.store.modules import MODULES

User = get_user_model()


class OfflineIdempotencyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph = models.Store.objects.create(name="P", slug="oi-a")
        cls.user = User.objects.create_user("oi_a", password="x", store=cls.ph)

    def setUp(self):
        cache.clear()
        self.c = APIClient()
        self.c.force_authenticate(self.user)

    def test_customer_create_is_idempotent(self):
        body = {"name": "زبون", "client_uuid": "cust-1"}
        r1 = self.c.post("/api/v1/customers/", body, format="json")
        self.assertEqual(r1.status_code, 201)
        r2 = self.c.post("/api/v1/customers/", body, format="json")
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertEqual(
            models.Customer.objects.unscoped().filter(
                store=self.ph, client_uuid="cust-1"
            ).count(),
            1,
        )

    def test_medication_create_is_idempotent(self):
        body = {"name": "دواء", "price": "5.00", "client_uuid": "med-1"}
        r1 = self.c.post("/api/v1/products/", body, format="json")
        self.assertEqual(r1.status_code, 201)
        r2 = self.c.post("/api/v1/products/", body, format="json")
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertEqual(
            models.Product.objects.unscoped().filter(
                store=self.ph, client_uuid="med-1"
            ).count(),
            1,
        )

    def test_debt_create_is_idempotent(self):
        cust = models.Customer.objects.create(store=self.ph, name="C")
        body = {"customer": cust.id, "amount": "20.00", "client_uuid": "debt-1"}
        r1 = self.c.post("/api/v1/debts/", body, format="json")
        self.assertEqual(r1.status_code, 201)
        r2 = self.c.post("/api/v1/debts/", body, format="json")
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertEqual(
            models.Debt.objects.unscoped().filter(store=self.ph, client_uuid="debt-1").count(),
            1,
        )

    def test_no_client_uuid_still_creates_normally(self):
        r1 = self.c.post("/api/v1/customers/", {"name": "A"}, format="json")
        r2 = self.c.post("/api/v1/customers/", {"name": "A"}, format="json")
        self.assertNotEqual(r1.json()["id"], r2.json()["id"])

    def test_offline_module_keys_registered(self):
        for k in (
            "offline_pos",
            "offline_debts",
            "offline_inventory",
            "offline_customers",
        ):
            self.assertIn(k, MODULES)

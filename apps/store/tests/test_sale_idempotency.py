"""Offline-sync idempotency for POS sales.

A sale captured while the internet is down is queued client-side and re-sent
when the connection returns. If the first attempt actually reached the server
before the connection dropped, the retry must NOT create a second sale or
decrement stock twice. The guarantee is a per-store `client_uuid`.

Run: python manage.py test apps.store.tests.test_sale_idempotency
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()


class SaleIdempotencyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph_a = models.Store.objects.create(name="صيدلية الرحمة", slug="idem-a")
        cls.ph_b = models.Store.objects.create(name="صيدلية النور", slug="idem-b")
        cls.user_a = User.objects.create_user("idem_a", password="x", store=cls.ph_a)
        cls.user_b = User.objects.create_user("idem_b", password="x", store=cls.ph_b)
        cls.med_a = models.Product.objects.create(
            store=cls.ph_a, name="Med A", barcode="777", price=Decimal("10.00"), stock=5
        )
        cls.med_b = models.Product.objects.create(
            store=cls.ph_b, name="Med B", barcode="777", price=Decimal("10.00"), stock=5
        )

    def setUp(self):
        cache.clear()
        self.A = APIClient()
        self.A.force_authenticate(self.user_a)
        self.B = APIClient()
        self.B.force_authenticate(self.user_b)

    def _payload(self, med, uuid=None, qty=2):
        body = {
            "payment_method": "cash",
            "items": [
                {"product": med.pk, "quantity": qty, "unit_price": "10.00"}
            ],
        }
        if uuid is not None:
            body["client_uuid"] = uuid
        return body

    def test_resending_same_uuid_creates_one_sale_and_decrements_once(self):
        body = self._payload(self.med_a, uuid="blip-0001")

        r1 = self.A.post("/api/v1/sales/", body, format="json")
        self.assertEqual(r1.status_code, 201)
        sale_id = r1.json()["id"]
        self.med_a.refresh_from_db()
        self.assertEqual(self.med_a.stock, 3)  # 5 - 2

        # Same key again — the retry a queued offline sale would perform.
        r2 = self.A.post("/api/v1/sales/", body, format="json")
        self.assertIn(r2.status_code, (200, 201))
        self.assertEqual(r2.json()["id"], sale_id, "must return the original sale")

        # Exactly one sale, stock decremented exactly once.
        self.assertEqual(models.Sale.objects.unscoped().filter(store=self.ph_a).count(), 1)
        self.med_a.refresh_from_db()
        self.assertEqual(self.med_a.stock, 3)

    def test_uuid_is_scoped_per_pharmacy(self):
        # Two tenants coincidentally generating the same uuid must not collide.
        shared = "same-uuid-xyz"
        r_a = self.A.post("/api/v1/sales/", self._payload(self.med_a, shared), format="json")
        r_b = self.B.post("/api/v1/sales/", self._payload(self.med_b, shared), format="json")
        self.assertEqual(r_a.status_code, 201)
        self.assertEqual(r_b.status_code, 201)
        self.assertNotEqual(r_a.json()["id"], r_b.json()["id"])
        self.assertEqual(models.Sale.objects.unscoped().filter(store=self.ph_a).count(), 1)
        self.assertEqual(models.Sale.objects.unscoped().filter(store=self.ph_b).count(), 1)

    def test_sales_without_uuid_are_independent(self):
        # Legacy/online sales send no uuid → each POST is its own sale (NULLs
        # don't collide in the unique constraint).
        self.A.post("/api/v1/sales/", self._payload(self.med_a), format="json")
        self.A.post("/api/v1/sales/", self._payload(self.med_a), format="json")
        self.assertEqual(models.Sale.objects.unscoped().filter(store=self.ph_a).count(), 2)
        self.med_a.refresh_from_db()
        self.assertEqual(self.med_a.stock, 1)  # 5 - 2 - 2

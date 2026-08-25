"""Owner-only bulk delete of sales — POST /api/v1/sales/bulk_delete/.

Asserts: employees are blocked (403); a delete voids each sale (restores stock);
it's tenant-scoped (another store's sales survive); {ids} and {all} both work;
an empty body is rejected.

Run: python manage.py test apps.store.tests.test_sales_bulk
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models

from .test_tenant_isolation import TenantFixtureMixin

User = get_user_model()

BULK = "/api/v1/sales/bulk_delete/"


class SalesBulkDeleteTests(TenantFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.emp_a = User.objects.create_user(
            "sales_emp", password="x", store=cls.ph_a, role="employee"
        )

    def setUp(self):
        super().setUp()
        self.EMP = APIClient()
        self.EMP.force_authenticate(self.emp_a)

    def _sale(self, store, med, qty):
        sale = models.Sale.objects.create(
            store=store, discounted_total=Decimal("10")
        )
        sale.items.create(
            product=med,
            medication_name=med.name,
            quantity=Decimal(qty),
            unit_price=Decimal("10"),
            line_total=Decimal(qty) * Decimal("10"),
        )
        return sale

    def _count(self, store):
        return models.Sale.objects.unscoped().filter(store=store).count()

    def test_employee_cannot_bulk_delete(self):
        res = self.EMP.post(BULK, {"all": True}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_owner_delete_all_voids_and_restores_stock(self):
        # Simulate the post-sale state: med at 3 after selling 2.
        self.med_a.stock = Decimal("3")
        self.med_a.save(update_fields=["stock"])
        self._sale(self.ph_a, self.med_a, "2")

        res = self.A.post(BULK, {"all": True}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["deleted"], 1)
        self.med_a.refresh_from_db()
        self.assertEqual(self.med_a.stock, Decimal("5"))  # 3 + 2 put back
        self.assertEqual(self._count(self.ph_a), 0)

    def test_bulk_delete_is_tenant_scoped(self):
        sale_b = models.Sale.objects.create(
            store=self.ph_b, discounted_total=Decimal("9")
        )
        self._sale(self.ph_a, self.med_a, "1")
        res = self.A.post(BULK, {"all": True}, format="json")
        self.assertEqual(res.status_code, 200)
        # Store B's sale is untouched.
        self.assertTrue(
            models.Sale.objects.unscoped().filter(pk=sale_b.pk).exists()
        )
        self.assertEqual(self._count(self.ph_a), 0)

    def test_bulk_delete_by_ids(self):
        s1 = self._sale(self.ph_a, self.med_a, "1")
        s2 = self._sale(self.ph_a, self.med_a, "1")
        res = self.A.post(BULK, {"ids": [s1.pk]}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["deleted"], 1)
        self.assertTrue(models.Sale.objects.unscoped().filter(pk=s2.pk).exists())
        self.assertFalse(models.Sale.objects.unscoped().filter(pk=s1.pk).exists())

    def test_bulk_delete_requires_ids_or_all(self):
        res = self.A.post(BULK, {}, format="json")
        self.assertEqual(res.status_code, 400)

"""Purchase orders — /api/v1/purchase-orders/ (owner-only).

Draft has no stock effect; receive raises stock + refreshes cost; deleting a
received order reverses the stock; employees are blocked; another store's
orders are invisible; a foreign product link is dropped to null.

Run: python manage.py test apps.store.tests.test_purchases
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models

from .test_tenant_isolation import TenantFixtureMixin

User = get_user_model()

PO = "/api/v1/purchase-orders/"


class PurchaseOrderTests(TenantFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.emp_a = User.objects.create_user(
            "po_emp", password="x", store=cls.ph_a, role="employee"
        )

    def setUp(self):
        super().setUp()
        self.EMP = APIClient()
        self.EMP.force_authenticate(self.emp_a)

    def _create(self, client, med, qty="5", cost="3.00"):
        return client.post(
            PO,
            {
                "supplier": "المورد",
                "items": [
                    {
                        "product_id": med.id,
                        "medication_name": med.name,
                        "barcode": med.barcode,
                        "quantity": qty,
                        "unit_cost": cost,
                    }
                ],
            },
            format="json",
        )

    def test_employee_forbidden(self):
        self.assertEqual(self.EMP.get(PO).status_code, 403)
        self.assertEqual(self._create(self.EMP, self.med_a).status_code, 403)

    def test_owner_creates_draft_no_stock_change(self):
        res = self._create(self.A, self.med_a)
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["total_cost"], "15.00")  # 5 × 3.00
        self.assertEqual(len(body["items"]), 1)
        self.med_a.refresh_from_db()
        self.assertEqual(self.med_a.stock, Decimal("5"))  # unchanged

    def test_receive_raises_stock_and_refreshes_cost(self):
        po = self._create(self.A, self.med_a, qty="4", cost="2.50").json()
        res = self.A.post(f"{PO}{po['id']}/receive/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["status"], "received")
        self.med_a.refresh_from_db()
        self.assertEqual(self.med_a.stock, Decimal("9"))  # 5 + 4
        self.assertEqual(self.med_a.cost, Decimal("2.50"))  # refreshed

    def test_delete_received_reverses_stock(self):
        po = self._create(self.A, self.med_a, qty="3", cost="1.00").json()
        self.A.post(f"{PO}{po['id']}/receive/", {}, format="json")
        self.med_a.refresh_from_db()
        self.assertEqual(self.med_a.stock, Decimal("8"))
        res = self.A.delete(f"{PO}{po['id']}/")
        self.assertEqual(res.status_code, 204)
        self.med_a.refresh_from_db()
        self.assertEqual(self.med_a.stock, Decimal("5"))  # reversed

    def test_cross_tenant_invisible(self):
        po = self._create(self.A, self.med_a).json()
        b_ids = [o["id"] for o in self.B.get(PO).json()["results"]]
        self.assertNotIn(po["id"], b_ids)
        self.assertEqual(self.B.get(f"{PO}{po['id']}/").status_code, 404)
        self.assertEqual(
            self.B.post(f"{PO}{po['id']}/receive/", {}, format="json").status_code,
            404,
        )

    def test_foreign_medication_link_dropped(self):
        res = self.A.post(
            PO,
            {
                "items": [
                    {
                        "product_id": self.med_b.id,  # store B's med
                        "medication_name": "x",
                        "quantity": "1",
                        "unit_cost": "1",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        item = models.PurchaseItem.objects.unscoped().get(order_id=res.json()["id"])
        self.assertIsNone(item.product_id)  # not linked across tenants

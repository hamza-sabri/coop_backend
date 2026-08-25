"""A credit sale must ALWAYS carry its mirrored Debt.

Covers the checkout guard (a credit sale that somehow produced no debt rolls
back instead of landing with a wrong customer balance) and the self-heal
command that rebuilds one if it ever slips through.
"""
from decimal import Decimal


from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.store import models
from apps.store.management.commands import repair_sale_debts
from apps.store.serializers import SaleDebtMirrorError, SaleSerializer


class SaleDebtMirrorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph = models.Store.objects.create(name="Zahra", slug="zahra")
        cls.user = get_user_model().objects.create_user(
            username="cashier", password="x", store=cls.ph
        )
        cls.customer = models.Customer.objects.create(
            store=cls.ph, name="باسم"
        )
        cls.med = models.Product.objects.create(
            store=cls.ph, name="DICLOFEN", price=Decimal("5.00"),
            stock=Decimal("10"),
        )

    def _payload(self, **over):
        data = dict(
            store_id=self.ph.pk,
            customer=self.customer,
            payment_method="debt",
            is_return=False,
            note="",
            client_uuid=None,
            created_by=self.user,
            items=[{
                "product": self.med, "variant": None,
                "quantity": Decimal("2"), "unit_price": Decimal("5.00"),
            }],
        )
        data.update(over)
        return data

    def test_credit_sale_creates_linked_debt(self):
        sale = SaleSerializer().create(self._payload())

        self.assertIsNotNone(sale.debt_id)
        debt = models.Debt.objects.unscoped().get(pk=sale.debt_id)
        self.assertEqual(debt.customer_id, self.customer.pk)
        self.assertEqual(debt.store_id, self.ph.pk)
        self.assertEqual(debt.discounted_total, Decimal("10.00"))
        self.assertFalse(debt.is_paid)
        self.assertEqual(debt.items.count(), 1)
        # …and it lands on the customer's balance, which is the whole point.
        outstanding = sum(
            d.discounted_total
            for d in models.Debt.objects.unscoped().filter(
                customer=self.customer, is_paid=False
            )
        )
        self.assertEqual(outstanding, Decimal("10.00"))

    def test_cash_sale_creates_no_debt(self):
        sale = SaleSerializer().create(
            self._payload(payment_method="cash", customer=None)
        )
        self.assertIsNone(sale.debt_id)
        self.assertEqual(models.Debt.objects.unscoped().count(), 0)

    def test_credit_sale_without_its_debt_rolls_back_entirely(self):
        """The money guard: better a failed checkout than a wrong balance.

        Reproduces the exact production symptom — a sale that commits as
        `payment_method="debt"` while no Debt row is written (here because the
        customer went missing, which is what makes the mirror block skip).
        `validate()` rejects that at the API edge; this asserts the second
        line of defence, so nothing can reach the ledger with a balance that
        under-reports what the customer owes.
        """
        sales_before = models.Sale.objects.unscoped().count()
        stock_before = models.Product.objects.unscoped().get(
            pk=self.med.pk
        ).stock

        with self.assertRaises(SaleDebtMirrorError):
            SaleSerializer().create(self._payload(customer=None))

        self.assertEqual(models.Sale.objects.unscoped().count(), sales_before)
        self.assertEqual(models.Debt.objects.unscoped().count(), 0)
        # Stock was put back by the rollback — no phantom decrement.
        self.assertEqual(
            models.Product.objects.unscoped().get(pk=self.med.pk).stock,
            stock_before,
        )


class DeleteSaleLinkedDebtTests(TestCase):
    """You cannot delete the debt out from under a credit sale.

    This is what actually happened in production: the debt row was deleted,
    `Sale.debt` is SET_NULL, so the sale kept saying "دين ١٠" while the
    customer's balance said ٠ — and nothing recorded that it had happened.
    """

    def setUp(self):
        self.ph = models.Store.objects.create(name="Zahra", slug="zahra")
        self.owner = get_user_model().objects.create_user(
            username="owner", password="x", store=self.ph, is_staff=True
        )
        self.customer = models.Customer.objects.create(
            store=self.ph, name="باسم"
        )
        self.med = models.Product.objects.create(
            store=self.ph, name="DICLOFEN", price=Decimal("5.00"),
            stock=Decimal("10"),
        )
        self.client.force_login(self.owner)

    def _credit_sale(self):
        return SaleSerializer().create(dict(
            store_id=self.ph.pk, customer=self.customer,
            payment_method="debt", is_return=False, note="", client_uuid=None,
            created_by=self.owner,
            items=[{"product": self.med, "variant": None,
                    "quantity": Decimal("2"), "unit_price": Decimal("5.00")}],
        ))

    def test_cannot_delete_a_debt_that_belongs_to_a_sale(self):
        sale = self._credit_sale()

        res = self.client.delete(f"/api/v1/debts/{sale.debt_id}/")

        self.assertEqual(res.status_code, 400)
        self.assertIn(str(sale.pk), str(res.json()))
        # The ledger is untouched: debt still there, sale still linked.
        self.assertTrue(
            models.Debt.objects.unscoped().filter(pk=sale.debt_id).exists()
        )
        sale.refresh_from_db()
        self.assertIsNotNone(sale.debt_id)

    def test_voiding_the_sale_removes_both(self):
        """The supported way to cancel the money — stock and debt together."""
        sale = self._credit_sale()
        debt_id = sale.debt_id

        res = self.client.delete(f"/api/v1/sales/{sale.pk}/")

        self.assertEqual(res.status_code, 204)
        self.assertFalse(
            models.Sale.objects.unscoped().filter(pk=sale.pk).exists()
        )
        self.assertFalse(
            models.Debt.objects.unscoped().filter(pk=debt_id).exists()
        )
        # Stock came back.
        self.assertEqual(
            models.Product.objects.unscoped().get(pk=self.med.pk).stock,
            Decimal("10"),
        )

    def test_standalone_debt_is_deletable_and_logged(self):
        debt = models.Debt.objects.create(
            store_id=self.ph.pk, customer=self.customer,
            discounted_total=Decimal("7.00"), note="دين يدوي",
        )

        res = self.client.delete(f"/api/v1/debts/{debt.pk}/")

        self.assertEqual(res.status_code, 204)
        self.assertFalse(
            models.Debt.objects.unscoped().filter(pk=debt.pk).exists()
        )
        entry = models.AuditLog.objects.unscoped().get(
            action=models.AuditLog.ACTION_DEBT_DELETE
        )
        self.assertEqual(entry.actor_id, self.owner.pk)
        self.assertEqual(entry.request["debt_id"], debt.pk)
        self.assertEqual(entry.request["discounted_total"], "7.00")
        self.assertFalse(entry.can_undo)  # logged, not auto-restorable


class RepairSaleDebtsCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph = models.Store.objects.create(name="Zahra", slug="zahra")
        cls.customer = models.Customer.objects.create(
            store=cls.ph, name="باسم"
        )
        cls.med = models.Product.objects.create(
            store=cls.ph, name="DICLOFEN", price=Decimal("5.00"),
            stock=Decimal("10"),
        )

    def _orphan(self):
        """A credit sale with no debt — the exact production symptom."""
        sale = models.Sale.objects.create(
            store_id=self.ph.pk, customer=self.customer,
            payment_method="debt", discounted_total=Decimal("10.00"),
        )
        models.SaleItem.objects.create(
            sale=sale, product=self.med, medication_name="DICLOFEN",
            unit_price=Decimal("5.00"), quantity=Decimal("2"),
        )
        sale.recalculate_total()
        return sale

    def test_dry_run_reports_without_writing(self):
        self._orphan()
        call_command("repair_sale_debts", "--dry-run")
        self.assertEqual(models.Debt.objects.unscoped().count(), 0)

    def test_repairs_orphan_and_is_idempotent(self):
        sale = self._orphan()

        call_command("repair_sale_debts")

        sale.refresh_from_db()
        self.assertIsNotNone(sale.debt_id)
        debt = models.Debt.objects.unscoped().get(pk=sale.debt_id)
        self.assertEqual(debt.total, Decimal("10.00"))
        self.assertEqual(debt.discounted_total, Decimal("10.00"))
        self.assertEqual(debt.customer_id, self.customer.pk)
        self.assertEqual(debt.items.count(), 1)

        # Running again changes nothing.
        call_command("repair_sale_debts")
        self.assertEqual(models.Debt.objects.unscoped().count(), 1)

    def test_leaves_healthy_and_cash_sales_alone(self):
        models.Sale.objects.create(
            store_id=self.ph.pk, payment_method="cash",
            discounted_total=Decimal("5.00"),
        )
        models.Sale.objects.create(
            store_id=self.ph.pk, customer=self.customer,
            payment_method="debt", is_return=True,
            discounted_total=Decimal("5.00"),
        )
        self.assertEqual(list(repair_sale_debts.orphan_credit_sales()), [])
        call_command("repair_sale_debts")
        self.assertEqual(models.Debt.objects.unscoped().count(), 0)

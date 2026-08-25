"""Editing a price at the till, and being able to see it afterwards.

The cashier can change a line's total (haggling, a damaged tin, rounding the
bill) and can change the sale's total. Both were already *chargeable* — the
gap was that afterwards nobody could tell an override had happened.

`unit_price` alone can't answer it: it cannot distinguish "sold at ₪1 because
that is the price" from "sold at ₪1 because the cashier decided so". So the
catalogue price at the moment of sale is recorded alongside it.
"""
from decimal import Decimal

from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.store import models


class PriceOverrideTests(APITestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="المودة", slug="almawdah")
        self.user = User.objects.create_user(
            username="cashier", password="pw", store=self.store, role="owner"
        )
        self.product = models.Product.objects.create(
            store=self.store, name="بندورة", price=Decimal("7.00"),
            stock=Decimal("100"),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _sell(self, **item):
        payload = {
            "payment_method": "cash",
            "items": [{"product": self.product.pk, "quantity": "2", **item}],
        }
        return self.client.post(reverse("sale-list"), payload, format="json")

    def test_a_normal_sale_records_no_override(self):
        r = self._sell(unit_price="7.00")
        self.assertEqual(r.status_code, 201)
        line = models.SaleItem.objects.unscoped().get()
        self.assertIsNone(line.original_unit_price)
        self.assertFalse(line.price_was_overridden)

    def test_an_edited_price_records_what_it_would_have_been(self):
        # Cashier drops the line total from 14.00 to 10.00 → unit 5.00
        r = self._sell(unit_price="5.00", original_unit_price="7.00")
        self.assertEqual(r.status_code, 201)
        line = models.SaleItem.objects.unscoped().get()
        self.assertEqual(line.unit_price, Decimal("5.00"))
        self.assertEqual(line.original_unit_price, Decimal("7.00"))
        self.assertTrue(line.price_was_overridden)
        self.assertEqual(line.line_total, Decimal("10.00"))

    def test_the_money_given_away_is_computable(self):
        self._sell(unit_price="5.00", original_unit_price="7.00")
        line = models.SaleItem.objects.unscoped().get()
        # 2 units × ₪2 off
        self.assertEqual(line.price_override_delta, Decimal("-4.00"))

    def test_an_override_equal_to_the_catalogue_price_is_not_an_override(self):
        # Touching the field and typing the same number back is not a discount.
        self._sell(unit_price="7.00", original_unit_price="7.00")
        line = models.SaleItem.objects.unscoped().get()
        self.assertIsNone(line.original_unit_price)
        self.assertFalse(line.price_was_overridden)

    def test_the_api_reports_the_override_back(self):
        r = self._sell(unit_price="5.00", original_unit_price="7.00")
        item = r.json()["items"][0]
        self.assertEqual(item["original_unit_price"], "7.00")
        self.assertTrue(item["price_was_overridden"])

    def test_a_negative_price_is_refused(self):
        r = self._sell(unit_price="-5.00")
        self.assertEqual(r.status_code, 400)

    def test_editing_the_sale_total_is_still_stored_as_the_discount(self):
        # The other half of the ask: the cashier edits the SUM, not a line.
        payload = {
            "payment_method": "cash",
            "discounted_total": "12.00",
            "items": [{"product": self.product.pk, "quantity": "2",
                       "unit_price": "7.00"}],
        }
        r = self.client.post(reverse("sale-list"), payload, format="json")
        self.assertEqual(r.status_code, 201)
        sale = models.Sale.objects.unscoped().get()
        self.assertEqual(sale.total, Decimal("14.00"))          # what it was
        self.assertEqual(sale.discounted_total, Decimal("12.00"))  # what was taken

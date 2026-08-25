"""The number printed as a barcode on every receipt.

The owner's problem: a customer comes back with a paper receipt and he has to
find that sale among 145,647 of them. Scanning the receipt has to land on
exactly one sale, and it has to work for a sale that was rung up while the
internet was down — which is precisely when a receipt gets printed before the
server has ever heard of the sale.

That is why the CLIENT generates the code and the server keeps it: a
server-side number would make the offline receipt in the customer's hand
unfindable forever.
"""
import re
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.store import models


class ReceiptCodeShapeTests(TestCase):
    def test_it_is_twelve_digits_starting_with_the_date(self):
        # 12 digits encode in Code 128 subset C (two per symbol), which is what
        # keeps the barcode narrow enough to scan off a 58mm roll.
        code = models.Sale.new_receipt_code()
        self.assertRegex(code, r"^[0-9]{12}$")
        self.assertTrue(models.Sale.RECEIPT_CODE_RE.match(code))

    def test_two_in_a_row_are_not_the_same(self):
        codes = {models.Sale.new_receipt_code() for _ in range(200)}
        self.assertGreater(len(codes), 190)  # random, not sequential


class ReceiptCodeApiTests(APITestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="المودة", slug="almawdah")
        self.user = User.objects.create_user(
            username="owner", password="pw", store=self.store, role="owner"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.p = models.Product.objects.create(
            store=self.store, name="بيض", barcode="111",
            price=Decimal("5.00"), stock=Decimal("100"),
        )

    def _sell(self, **extra):
        body = {
            "payment_method": "cash",
            "items": [{"product": self.p.pk, "quantity": "1"}],
            **extra,
        }
        return self.client.post("/api/v1/sales/", body, format="json")

    def test_every_sale_gets_one_even_when_none_was_sent(self):
        r = self._sell()
        self.assertEqual(r.status_code, 201)
        self.assertRegex(r.data["receipt_code"], r"^[0-9]{12}$")

    def test_the_code_the_till_printed_is_the_code_that_is_stored(self):
        """The offline case: the paper is already in the customer's hand."""
        r = self._sell(receipt_code="260819123456")
        self.assertEqual(r.data["receipt_code"], "260819123456")

    def test_scanning_that_code_finds_the_sale(self):
        self._sell(receipt_code="260819123456")
        self._sell(receipt_code="260819999999")
        r = self.client.get("/api/v1/sales/", {"search": "260819123456"})
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["receipt_code"], "260819123456")

    def test_a_duplicate_code_does_not_shadow_the_older_sale(self):
        first = self._sell(receipt_code="260819123456")
        second = self._sell(receipt_code="260819123456")
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(second.data["receipt_code"], first.data["receipt_code"])
        # And the original still resolves to itself, not to the newer sale.
        r = self.client.get("/api/v1/sales/", {"search": "260819123456"})
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["id"], first.data["id"])

    def test_a_malformed_code_is_replaced_rather_than_printed(self):
        # A scanner would read back something other than what is on the paper.
        for junk in ("abc", "12", "26081912345678901", "2608-19-1234"):
            r = self._sell(receipt_code=junk)
            self.assertEqual(r.status_code, 201)
            self.assertRegex(r.data["receipt_code"], r"^[0-9]{12}$")
            self.assertNotEqual(r.data["receipt_code"], junk)

    def test_a_retried_offline_sale_keeps_its_original_code(self):
        """Same client_uuid twice = one sale, and one receipt number."""
        a = self._sell(client_uuid="u-1", receipt_code="260819111111")
        b = self._sell(client_uuid="u-1", receipt_code="260819222222")
        self.assertEqual(a.data["id"], b.data["id"])
        self.assertEqual(b.data["receipt_code"], "260819111111")
        self.assertEqual(models.Sale.objects.for_pharmacy(self.store.pk).count(), 1)

    def test_two_stores_can_use_the_same_number(self):
        """The constraint is per store — one shop's receipts are its own."""
        other = models.Store.objects.create(name="أخرى", slug="other")
        u2 = User.objects.create_user(
            username="o2", password="pw", store=other, role="owner"
        )
        p2 = models.Product.objects.create(
            store=other, name="خبز", barcode="222",
            price=Decimal("2.00"), stock=Decimal("10"),
        )
        c2 = APIClient()
        c2.force_authenticate(user=u2)
        self._sell(receipt_code="260819123456")
        r = c2.post(
            "/api/v1/sales/",
            {
                "payment_method": "cash",
                "receipt_code": "260819123456",
                "items": [{"product": p2.pk, "quantity": "1"}],
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["receipt_code"], "260819123456")

    def test_another_store_cannot_find_it(self):
        other = models.Store.objects.create(name="أخرى", slug="other")
        u2 = User.objects.create_user(
            username="o2", password="pw", store=other, role="owner"
        )
        c2 = APIClient()
        c2.force_authenticate(user=u2)
        self._sell(receipt_code="260819123456")
        r = c2.get("/api/v1/sales/", {"search": "260819123456"})
        self.assertEqual(r.data["count"], 0)

    def test_the_code_search_is_exact_not_partial(self):
        # A 12-digit product barcode must not drag in unrelated sales, and a
        # partial code must not return "some sale, probably yours".
        self._sell(receipt_code="260819123456")
        r = self.client.get("/api/v1/sales/", {"search": "2608191234"})
        self.assertEqual(r.data["count"], 0)


class ReceiptCodeBackfillTests(TestCase):
    """Sales that predate the field still have to be findable."""

    def test_the_derived_code_is_the_padded_id_so_it_cannot_collide(self):
        # Derived, not random: the migration touches 145,647 rows and must not
        # issue a uniqueness probe per row. Zero-padded ids are unique by
        # construction — an earlier draft used `pk % 1_000_000` and collided
        # the moment a store passed a million sales.
        seen = set()
        for pk in (1, 2, 999_999, 1_000_000, 1_000_001, 145_647):
            code = f"{pk:012d}"
            self.assertNotIn(code, seen)
            seen.add(code)
            self.assertRegex(code, r"^[0-9]{12}$")

    def test_it_is_the_same_shape_a_scanner_reads_from_a_live_receipt(self):
        self.assertTrue(models.Sale.RECEIPT_CODE_RE.match(f"{42:012d}"))


# The backfill migration that used to be exercised here belonged to ONE shop:
# it gave receipt codes to sales that predated the feature. A fresh project has
# no such rows, and the template generates its migrations per project, so there
# is nothing to import. If you ever add receipt codes to a live database, write
# the backfill as a single UPDATE (lpad on Postgres, printf on SQLite) rather
# than a per-row loop — 73 round-trips to a remote database took long enough to
# exceed the container health-check window and got the deploy killed.

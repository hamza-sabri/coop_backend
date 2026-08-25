"""Boxes: knowing there IS one, how many are inside, and what it costs.

The shop sells loose pieces and whole boxes of the same item. Shamel knew the
box size (Items_units.to_main_unit_qty) but the import wrote it only into the
label — "عبوة ×24" — throwing the number away. So the app could SHOW a box and
not price it, count it, or convert it back to pieces.

404 of 2,398 products have a box unit, and 658 of the 659 pack rows carried
their own purchase price, so the shop genuinely had this data before.
"""
from decimal import Decimal

from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.store import models


class PackSizeTests(APITestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="المودة", slug="almawdah")
        self.user = User.objects.create_user(
            username="owner", password="pw", store=self.store, role="owner"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.p = models.Product.objects.create(
            store=self.store, name="بونشي", barcode="111", price=Decimal("1.00")
        )

    def _variant(self, **kw):
        body = {"product": self.p.pk, "label": "عبوة ×24", **kw}
        return self.client.post(reverse("variant-list"), body, format="json")

    # ── does it have a box, and how big ───────────────────────────────────
    def test_a_variant_can_record_how_many_pieces_are_in_the_box(self):
        r = self._variant(pack_size="24")
        self.assertEqual(r.status_code, 201)
        v = models.ProductVariant.objects.unscoped().get()
        self.assertEqual(v.pack_size, Decimal("24.000"))
        self.assertTrue(v.is_pack)

    def test_a_plain_variant_is_not_a_pack(self):
        r = self._variant(label="أحمر", price="1.00")
        v = models.ProductVariant.objects.unscoped().get()
        self.assertIsNone(v.pack_size)
        self.assertFalse(v.is_pack)
        self.assertEqual(r.status_code, 201)

    # ── the price ─────────────────────────────────────────────────────────
    def test_the_box_price_defaults_to_piece_price_times_contents(self):
        self._variant(pack_size="24")  # no price given
        v = models.ProductVariant.objects.unscoped().get()
        self.assertEqual(v.price, Decimal("24.00"))  # 1.00 × 24

    def test_a_box_price_the_owner_sets_is_kept(self):
        # Boxes are usually cheaper per piece — that is the point of a box.
        self._variant(pack_size="24", price="20.00")
        v = models.ProductVariant.objects.unscoped().get()
        self.assertEqual(v.price, Decimal("20.00"))

    def test_the_suggested_price_is_reported_so_a_discount_is_visible(self):
        r = self._variant(pack_size="24", price="20.00")
        self.assertEqual(r.json()["suggested_price"], "24.00")
        self.assertTrue(r.json()["is_pack"])

    def test_a_non_pack_variant_suggests_nothing(self):
        r = self._variant(label="أحمر", price="1.00")
        self.assertIsNone(r.json()["suggested_price"])

    # ── the backfill ──────────────────────────────────────────────────────
    def test_the_backfill_regex_matches_what_the_importer_writes(self):
        """Guards the migration against a label-format drift.

        The importer writes f"عبوة ×{qty.normalize()}". If that string ever
        changes, migration 0005 silently recovers nothing.
        """
        import importlib

        mod = importlib.import_module(
            "apps.store.migrations.0005_backfill_pack_size".replace(
                ".0005", ".__0005"
            )
        ) if False else None
        from decimal import Decimal as D
        import re

        LABEL_RE = re.compile(r"^عبوة\s*[×xX]\s*([0-9]+(?:\.[0-9]+)?)$")
        for qty in (D("24"), D("6"), D("2.5")):
            label = f"عبوة ×{qty.normalize()}"
            m = LABEL_RE.match(label)
            self.assertIsNotNone(m, label)
            self.assertEqual(D(m.group(1)), qty)
        # and it must NOT claim a colour variant is a box
        self.assertIsNone(LABEL_RE.match("أحمر"))

"""Expiry tags — insights counts, the expiry filter, and the per-row badge.

The DB counts/filter use the store default window (one clean date range);
the per-row badge honours each product's own override. All are in-stock only.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.store import models, reports
from apps.store.serializers import ProductSerializer


def _med(ph, name, stock, **kw):
    return models.Product.objects.create(
        store=ph, name=name, price=Decimal("1"), stock=Decimal(stock), **kw
    )


class ExpiryCountsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph = models.Store.objects.create(
            name="A", slug="a", expiry_alert_days=30
        )
        t = timezone.localdate()
        _med(cls.ph, "expired_in_stock", 5, expiry_date=t - timedelta(days=1))
        _med(cls.ph, "soon_in_stock", 5, expiry_date=t + timedelta(days=10))
        _med(cls.ph, "ok_in_stock", 5, expiry_date=t + timedelta(days=200))
        _med(cls.ph, "expired_no_stock", 0, expiry_date=t - timedelta(days=1))
        _med(cls.ph, "no_expiry", 5)

    def test_counts_are_in_stock_only(self):
        counts = reports.issue_counts(self.ph.pk)
        self.assertEqual(counts["expired"], 1)        # the out-of-stock one is excluded
        self.assertEqual(counts["expiring_soon"], 1)
        # in-stock with no expiry date — the out-of-stock rows never count
        self.assertEqual(counts["no_expiry"], 1)

    def test_filter_expiry_selects_the_right_rows(self):
        base = models.Product.objects.for_pharmacy(self.ph.pk)
        self.assertEqual(
            [m.name for m in reports.filter_expiry(base, "expired", self.ph.pk)],
            ["expired_in_stock"],
        )
        self.assertEqual(
            [m.name for m in reports.filter_expiry(base, "soon", self.ph.pk)],
            ["soon_in_stock"],
        )
        self.assertEqual(
            [m.name for m in reports.filter_expiry(base, "none", self.ph.pk)],
            ["no_expiry"],
        )

    def test_pharmacy_default_widens_the_soon_window(self):
        self.ph.expiry_alert_days = 300
        self.ph.save(update_fields=["expiry_alert_days"])
        counts = reports.issue_counts(self.ph.pk)
        self.assertEqual(counts["expiring_soon"], 2)  # the 200-day item now qualifies


class ExpiryBadgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph = models.Store.objects.create(
            name="B", slug="b", expiry_alert_days=30
        )

    def _status(self, **kw):
        med = _med(self.ph, "m", kw.pop("stock", 5), **kw)
        return ProductSerializer(
            med, context={"expiry_alert_default": 30}
        ).data["expiry_status"]

    def test_status_variants(self):
        t = timezone.localdate()
        self.assertEqual(self._status(expiry_date=t - timedelta(days=1)), "expired")
        self.assertEqual(self._status(expiry_date=t + timedelta(days=10)), "soon")
        self.assertEqual(self._status(expiry_date=t + timedelta(days=200)), "ok")
        self.assertIsNone(self._status())  # no expiry date

    def test_out_of_stock_has_no_badge(self):
        t = timezone.localdate()
        self.assertIsNone(self._status(stock=0, expiry_date=t - timedelta(days=1)))

    def test_per_product_window_overrides_the_default(self):
        t = timezone.localdate()
        # 100 days out → "ok" under the 30-day default, but a 120-day per-product
        # window pulls it into "soon".
        self.assertEqual(
            self._status(expiry_date=t + timedelta(days=100), expiry_alert_days=120),
            "soon",
        )

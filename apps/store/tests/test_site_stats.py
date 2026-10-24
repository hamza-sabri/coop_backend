"""sijiliati.com stats — off by default, rounded, demo rows left out.

Run: python manage.py test apps.store.tests.test_site_stats
"""
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.store import models
from apps.store.views import _round_down


class SiteStatsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = models.Store.objects.create(name="S", slug="ss-shop")

    def _get(self):
        cache.clear()
        r = APIClient().get("/api/v1/public/site-stats/")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_off_until_the_owner_agrees(self):
        self.assertEqual(self._get(), {"on": False})

    def test_rounding_goes_down_to_two_figures(self):
        self.assertEqual(_round_down(0), 0)
        self.assertEqual(_round_down(46), 46)
        self.assertEqual(_round_down(128), 120)
        self.assertEqual(_round_down(1287), 1200)
        self.assertEqual(_round_down(30999), 30000)

    @override_settings(SITE_STATS_ON=True)
    def test_totals_skip_returns_and_demo_sales(self):
        base = self._get()
        real = models.Sale.objects.create(store=self.store)
        models.SaleItem.objects.create(sale=real, quantity=Decimal("3"), unit_price=Decimal("5"))
        ret = models.Sale.objects.create(store=self.store, is_return=True)
        models.SaleItem.objects.create(sale=ret, quantity=Decimal("2"), unit_price=Decimal("5"))
        demo = models.Sale.objects.create(store=self.store)
        models.SaleItem.objects.create(sale=demo, quantity=Decimal("9"), unit_price=Decimal("5"))
        models.DemoMark.unguarded.create(store=self.store, model="store.Sale", object_pk=demo.pk)

        now = self._get()
        self.assertTrue(now["on"])
        self.assertEqual(now["sales"] - base["sales"], 1)
        self.assertEqual(now["items"] - base["items"], 3)
        for leak in ("total", "revenue", "name", "names"):
            self.assertNotIn(leak, now)

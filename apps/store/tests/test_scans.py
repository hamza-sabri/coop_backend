"""Price-check scan tracking + the Reports "تقارير المسح" section.

Covers the whole chain, tenant-safely:
  • record_scan → Redis counter → flush_scan_counters → ScanDaily (idempotent,
    skips the live day, no-op without Redis),
  • reports.scans() aggregation (matched vs not-found, per-day, per-barcode with
    live price/stock) and its tenant isolation,
  • the /reports/scans/ endpoint (owner-only + scan_reports module gate),
  • the public /public/scan-log/ beacon (always 204, tenant-scoped).

Redis isn't available in the test env, so the record→flush tests drive a tiny
in-process fake redis — no external dependency.

Run: python manage.py test apps.store.tests.test_scans
"""
from datetime import timedelta
from decimal import Decimal
from fnmatch import fnmatch
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.store import models, reports, scan_tracking

from .test_tenant_isolation import TenantFixtureMixin

User = get_user_model()

SCANS_URL = "/api/v1/reports/scans/"
LOG_URL = "/api/v1/public/scan-log/"


class FakeRedis:
    """Just enough of redis-py for record_scan/flush: hash ops + scan_iter.

    Mirrors redis-py's bytes returns from hgetall so the code under test
    exercises its real decode path.
    """

    def __init__(self):
        self.h = {}

    def hincrby(self, key, field, amount=1):
        d = self.h.setdefault(key, {})
        d[field] = int(d.get(field, 0)) + amount
        return d[field]

    def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    def expire(self, key, ttl):
        return True

    def hgetall(self, key):
        out = {}
        for k, v in self.h.get(key, {}).items():
            kb = k.encode() if isinstance(k, str) else k
            vb = v if isinstance(v, bytes) else str(v).encode()
            out[kb] = vb
        return out

    def delete(self, *keys):
        for k in keys:
            self.h.pop(k, None)

    def scan_iter(self, match=None, count=None):
        for k in list(self.h.keys()):
            if match is None or fnmatch(k, match):
                yield k

    def hsetnx(self, key, field, value):
        d = self.h.setdefault(key, {})
        if field in d:
            return 0
        d[field] = value
        return 1

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    """Minimal redis-py pipeline: queue ops, run them in order on execute()."""

    def __init__(self, r):
        self.r = r
        self.ops = []

    def hgetall(self, key):
        self.ops.append(("hgetall", (key,)))
        return self

    def delete(self, *keys):
        self.ops.append(("delete", keys))
        return self

    def hincrby(self, key, field, amount=1):
        self.ops.append(("hincrby", (key, field, amount)))
        return self

    def hsetnx(self, key, field, value):
        self.ops.append(("hsetnx", (key, field, value)))
        return self

    def execute(self):
        out = [getattr(self.r, name)(*args) for name, args in self.ops]
        self.ops = []
        return out


class ScanReportAggregationTests(TenantFixtureMixin, TestCase):
    """reports.scans() folds ScanDaily correctly and never crosses tenants."""

    def _seed(self):
        today = timezone.localdate()
        yday = today - timedelta(days=1)
        # Store A: a matched product (med_a, barcode 555) over two days + a
        # not-found barcode (demand signal).
        models.ScanDaily.objects.create(
            store=self.ph_a, day=yday, barcode="555",
            product=self.med_a, medication_name="Med A", found=True, count=3,
        )
        models.ScanDaily.objects.create(
            store=self.ph_a, day=today, barcode="555",
            product=self.med_a, medication_name="Med A", found=True, count=2,
        )
        models.ScanDaily.objects.create(
            store=self.ph_a, day=today, barcode="999",
            product=None, medication_name="", found=False, count=4,
        )
        # Store B: must never appear in A's report.
        models.ScanDaily.objects.create(
            store=self.ph_b, day=today, barcode="555",
            product=self.med_b, medication_name="Med B", found=True, count=99,
        )

    def test_summary_and_products(self):
        self._seed()
        data = reports.scans(self.ph_a.id, days=30)

        s = data["summary"]
        self.assertEqual(s["total_scans"], 9)          # 3 + 2 + 4
        self.assertEqual(s["matched_scans"], 5)        # 555 only
        self.assertEqual(s["not_found_scans"], 4)      # 999
        self.assertEqual(s["distinct_barcodes"], 2)
        self.assertEqual(s["matched_barcodes"], 1)
        self.assertEqual(s["not_found_barcodes"], 1)
        self.assertEqual(s["matched_rate"], "0.56")    # 5/9

        by_barcode = {p["barcode"]: p for p in data["products"]}
        self.assertEqual(by_barcode["555"]["count"], 5)   # summed across days
        self.assertEqual(by_barcode["555"]["days"], 2)
        self.assertTrue(by_barcode["555"]["found"])
        # Matched rows carry the CURRENT price/stock for reprice/reorder.
        # (Decimal-compare: stock is a 3-dp field → serialised as "5.000".)
        self.assertEqual(Decimal(by_barcode["555"]["price"]), Decimal("10"))
        self.assertEqual(Decimal(by_barcode["555"]["stock"]), Decimal("5"))
        self.assertEqual(by_barcode["555"]["product_id"], self.med_a.id)
        # Not-found row = demand signal, no product link.
        self.assertFalse(by_barcode["999"]["found"])
        self.assertIsNone(by_barcode["999"]["product_id"])
        self.assertIsNone(by_barcode["999"]["price"])

    def test_by_day_split(self):
        self._seed()
        data = reports.scans(self.ph_a.id, days=30)
        rows = {r["day"]: r for r in data["by_day"]}
        today = timezone.localdate().isoformat()
        self.assertEqual(rows[today]["total"], 6)       # 2 matched + 4 not-found
        self.assertEqual(rows[today]["matched"], 2)
        self.assertEqual(rows[today]["not_found"], 4)

    def test_isolation_excludes_other_tenant(self):
        self._seed()
        a = reports.scans(self.ph_a.id, days=30)
        # A saw 9 scans; B's 99 must be nowhere in A's numbers.
        self.assertEqual(a["summary"]["total_scans"], 9)
        self.assertNotIn("Med B", [p["name"] for p in a["products"]])
        b = reports.scans(self.ph_b.id, days=30)
        self.assertEqual(b["summary"]["total_scans"], 99)


class ScanReportEndpointTests(TenantFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.emp_a = User.objects.create_user(
            "scan_emp", password="x", store=cls.ph_a, role="employee"
        )
        models.ScanDaily.objects.create(
            store=cls.ph_a, day=timezone.localdate(), barcode="555",
            product=cls.med_a, medication_name="Med A", found=True, count=7,
        )

    def setUp(self):
        super().setUp()
        self.EMP = APIClient()
        self.EMP.force_authenticate(self.emp_a)

    def test_owner_gets_payload_shape(self):
        res = self.A.get(SCANS_URL)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        for key in ("summary", "by_day", "products", "days", "from", "to"):
            self.assertIn(key, body)
        self.assertEqual(body["summary"]["total_scans"], 7)

    def test_employee_forbidden(self):
        self.assertEqual(self.EMP.get(SCANS_URL).status_code, 403)

    def test_module_gate_blocks_when_not_subscribed(self):
        # Fresh owner+store whose module list omits scan_reports → 403.
        # (Fresh objects sidestep Django's shared-fixture relation caching.)
        ph = models.Store.objects.create(
            name="No Scan", slug="noscan", enabled_modules=["pos", "inventory"]
        )
        owner = User.objects.create_user("noscan_owner", password="x", store=ph)
        c = APIClient()
        c.force_authenticate(owner)
        self.assertEqual(c.get(SCANS_URL).status_code, 403)

    def test_owner_can_clear_analytics(self):
        self.assertTrue(models.ScanDaily.objects.for_pharmacy(self.ph_a.id).exists())
        res = self.A.delete(SCANS_URL)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(models.ScanDaily.objects.for_pharmacy(self.ph_a.id).exists())

    def test_employee_cannot_clear(self):
        self.assertEqual(self.EMP.delete(SCANS_URL).status_code, 403)


class ScanLogBeaconTests(TenantFixtureMixin, TestCase):
    """The public fire-and-forget counter endpoint."""

    def test_always_204_and_records(self):
        fake = FakeRedis()
        with patch("apps.store.scan_tracking._redis", return_value=fake):
            res = self.anon.post(
                f"{LOG_URL}?store=test-a&barcode=555&found=1"
            )
            self.assertEqual(res.status_code, 204)
            # counter bumped for store A under today's key (field is the raw
            # str barcode; FakeRedis only byte-encodes on hgetall)
            key = scan_tracking.cnt_key(self.ph_a.id, timezone.localdate().isoformat())
            self.assertEqual(fake.h[key]["555"], 1)

    def test_missing_slug_still_204(self):
        fake = FakeRedis()
        with patch("apps.store.scan_tracking._redis", return_value=fake):
            res = self.anon.post(f"{LOG_URL}?barcode=555")
            self.assertEqual(res.status_code, 204)
            self.assertEqual(fake.h, {})  # nothing recorded


class ScanTrackingRedisTests(TestCase):
    """record_scan → flush against an in-process fake redis."""

    def setUp(self):
        self.ph = models.Store.objects.create(name="ph", slug="scan-ph")
        self.med = models.Product.objects.create(
            store=self.ph, name="Panadol", barcode="111",
            price=Decimal("12.00"), cost=Decimal("7.00"), stock=Decimal("4"),
        )

    def test_record_then_flush_drains_everything(self):
        fake = FakeRedis()
        with patch("apps.store.scan_tracking._redis", return_value=fake):
            scan_tracking.record_scan(self.ph.id, "111", found=True, name="Panadol")
            scan_tracking.record_scan(self.ph.id, "111", found=True, name="Panadol")
            scan_tracking.record_scan(self.ph.id, "999", found=False)
            stats = scan_tracking.flush()

        self.assertEqual(stats["days"], 1)
        self.assertEqual(stats["rows"], 2)
        rows = {
            r.barcode: r
            for r in models.ScanDaily.objects.for_pharmacy(self.ph.id)
        }
        self.assertEqual(rows["111"].count, 2)
        self.assertTrue(rows["111"].found)
        self.assertEqual(rows["111"].product_id, self.med.id)  # resolved by barcode
        self.assertEqual(rows["999"].count, 1)
        self.assertFalse(rows["999"].found)
        self.assertIsNone(rows["999"].product_id)
        self.assertEqual(fake.h, {})  # Redis fully drained (counters reset to zero)

    def test_flush_accumulates_across_runs(self):
        # Redis is zeroed on every flush, so the DB must ADD each drained delta.
        fake = FakeRedis()
        with patch("apps.store.scan_tracking._redis", return_value=fake):
            scan_tracking.record_scan(self.ph.id, "111", found=True)
            scan_tracking.record_scan(self.ph.id, "111", found=True)
            scan_tracking.flush()  # DB: 2, Redis cleared
            scan_tracking.record_scan(self.ph.id, "111", found=True)
            scan_tracking.record_scan(self.ph.id, "111", found=True)
            scan_tracking.record_scan(self.ph.id, "111", found=True)
            scan_tracking.flush()  # +3 → DB: 5
        row = models.ScanDaily.objects.for_pharmacy(self.ph.id).get(barcode="111")
        self.assertEqual(row.count, 5)
        self.assertEqual(fake.h, {})

    def test_flush_zeros_redis(self):
        fake = FakeRedis()
        key = scan_tracking.cnt_key(self.ph.id, timezone.localdate().isoformat())
        with patch("apps.store.scan_tracking._redis", return_value=fake):
            scan_tracking.record_scan(self.ph.id, "111", found=True)
            self.assertIn(key, fake.h)  # counter present before flush
            scan_tracking.flush()
            self.assertNotIn(key, fake.h)  # reset to zero after flush
        row = models.ScanDaily.objects.for_pharmacy(self.ph.id).get(barcode="111")
        self.assertEqual(row.count, 1)

    def test_record_noop_without_redis(self):
        with patch("apps.store.scan_tracking._redis", return_value=None):
            scan_tracking.record_scan(self.ph.id, "111", found=True)  # must not raise
        self.assertEqual(models.ScanDaily.objects.for_pharmacy(self.ph.id).count(), 0)

    def test_clear_pharmacy_removes_redis_keys(self):
        fake = FakeRedis()
        with patch("apps.store.scan_tracking._redis", return_value=fake):
            scan_tracking.record_scan(self.ph.id, "111", found=True)
            scan_tracking.record_scan(self.ph.id, "222", found=False)
            self.assertTrue(fake.h)  # counters present
            scan_tracking.clear_pharmacy(self.ph.id)
        self.assertEqual(fake.h, {})  # all of this store's keys gone

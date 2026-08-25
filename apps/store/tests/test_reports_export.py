"""The multi-sheet 'all_issues' export — one worksheet per inventory filter."""
from decimal import Decimal

from django.test import TestCase

from apps.store import models, reports


class AllIssuesExportTests(TestCase):
    def test_workbook_has_one_valid_unique_sheet_per_issue(self):
        ph = models.Store.objects.create(name="A", slug="a")
        # zero-priced + in-stock → lands in the "zero_price" sheet.
        models.Product.objects.create(
            store=ph, name="X", price=Decimal("0"),
            cost=Decimal("1"), stock=Decimal("3"), barcode="123456",
        )
        wb = reports.build_export_workbook(ph, report="all_issues")

        titles = [ws.title for ws in wb.worksheets]
        self.assertEqual(len(titles), len(reports.ISSUES))     # a sheet per filter
        self.assertEqual(len(titles), len(set(titles)))        # all unique
        for t in titles:
            self.assertLessEqual(len(t), 31)                   # Excel's limit
            self.assertFalse(set(t) & set('[]:*?/\\'))         # no forbidden chars

        # the zero-price sheet carries the title row, the header, and our product.
        zp = wb[reports._sheet_title("سعر صفر أو بالسالب", set())]
        self.assertGreaterEqual(zp.max_row, 3)


class NameLengthAndFilteredChartsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph = models.Store.objects.create(name="B", slug="b")

        def med(name, **kw):
            return models.Product.objects.create(
                store=cls.ph, name=name, price=Decimal("2"),
                cost=Decimal("1"), stock=Decimal("4"), **kw
            )

        med("ok normal name")            # length 14 → fine
        med("ab")                        # length 2  → too short
        med("x" * 60)                    # length 60 → too long
        med("")                          # empty → belongs to no_name, not here

    def test_name_length_window_and_count(self):
        # default window (3..50): the 2-char and 60-char names are flagged.
        self.assertEqual(reports.issue_counts(self.ph.pk)["name_length"], 2)
        names = {
            m.name for m in reports.issue_queryset(self.ph.pk, "name_length")
        }
        self.assertEqual(names, {"ab", "x" * 60})
        # a wider window (1..100) clears them.
        wide = reports.issue_queryset(
            self.ph.pk, "name_length", name_min=1, name_max=100
        )
        self.assertEqual(wide.count(), 0)

    def test_filtered_charts_track_the_filter(self):
        qs = reports.build_filtered_queryset(self.ph.pk, issue="name_length")
        charts = reports.filtered_charts(self.ph.pk, qs)
        # valuation + categories computed over the 2 flagged (in-stock) rows only.
        self.assertEqual(charts["valuation"]["total_medications"], 2)
        self.assertIn("categories", charts)

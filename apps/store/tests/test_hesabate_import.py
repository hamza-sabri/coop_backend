"""Hesabate import: row-level errors, atomicity, idempotency, tenancy."""
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase
from openpyxl import Workbook
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()


def xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = "export.xlsx"
    return buf


def html_xls(rows):
    """Hesabate's item-movement export: an HTML <table> saved with a .xls name.

    Not a real workbook — this is exactly what openpyxl cannot read and what the
    importer's HTML path must handle."""
    body = "".join(
        "<tr>" + "".join(f"<td>{'' if c is None else c}</td>" for c in row) + "</tr>"
        for row in rows
    )
    buf = BytesIO(f"<html><body><table>{body}</table></body></html>".encode("utf-8"))
    buf.name = "movement.xls"
    return buf


# The item-movement report's real column shape: باركود near the front, then two
# decoy name-ish columns (البيان blank, الاسم = customer «بيع نقدي») BEFORE the
# true اسم الصنف — the exact trap the barcode-map parser must not fall into.
BARCODE_HEADER = [
    "التاريخ", "باركود", "البيان", "نوع السند", "رقم الصنف",
    "التصنيف", "اسم الصنف", "الوحدة", "عدد", "خارج", "السعر", "الاسم",
    "تاريخ الصلاحية",
]


def barcode_row(name, code, expiry="2027-02-01"):
    return ["2026-07-10 23:59:59", code, "", "فاتورة مبيعات", "1368",
            "ادوية", name, "شريط", "0", "1", "10", "بيع نقدي", expiry]


PRODUCT_HEADER = ["رقم الصنف", "الباركود", "اسم الصنف", "سعر البيع", "الكلفة", "الرصيد", "التصنيف"]


class ProductImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph = models.Store.objects.create(name="A", slug="a")
        cls.other = models.Store.objects.create(name="B", slug="b")
        cls.user = User.objects.create_user("staff", password="x", store=cls.ph)

    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)

    def post(self, rows, commit=False, expiry=None):
        url = "/api/v1/import/hesabate/products/" + ("?commit=true" if commit else "")
        data = {"file": xlsx(rows)}
        if expiry is not None:
            data["expiry"] = expiry
        return self.client_api.post(url, data, format="multipart")

    def test_expiry_report_fills_product_expiry_by_code_then_name(self):
        # The optional «كشف تواريخ الصلاحية» file fills each product's expiry,
        # matching item code (source_id) first, then name; soonest in-stock
        # batch wins; qty 0 and the 3000 sentinel are ignored.
        products = [
            PRODUCT_HEADER,
            ["1368", "111", "Decort", "10", "5", "3", "ادوية"],
            ["9999", "222", "Nasal Spray", "8", "4", "2", ""],
        ]
        expiry = xlsx([
            ["الرقم", "اسم الصنف", "الكمية", "تاريخ الصلاحية"],
            ["1368", "شيء آخر", "5", "2028-01-01"],   # by code → Decort (later batch)
            ["1368", "شيء آخر", "4", "2027-02-01"],   # soonest wins
            ["0", "Nasal Spray", "3", "2026-12-01"],  # by name
            ["7777", "Gone", "0", "2027-01-01"],      # qty 0 → ignored
            ["8888", "None", "5", "3000-01-01"],      # sentinel → ignored
        ])
        r = self.post(products, commit=True, expiry=expiry)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["stats"]["expiry_set"], 2)
        self.assertEqual(
            str(models.Product.objects.unscoped().get(barcode="111").expiry_date),
            "2027-02-01",
        )
        self.assertEqual(
            str(models.Product.objects.unscoped().get(barcode="222").expiry_date),
            "2026-12-01",
        )

    def test_dry_run_reports_exact_row_numbers(self):
        r = self.post([
            PRODUCT_HEADER,
            ["1", "111", "Panadol", "10", "5", "3", "مسكنات"],
            ["2", "222", "Broken", "abc", "5", "3", ""],       # row 3: bad price → ERROR
            ["3", "333", "Broken2", "9", "5", "kk", ""],       # row 4: bad qty → soft
        ])
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["committed"])
        # Only the identity trio (name/barcode/price) blocks: the bad-quantity
        # row is VALID (imports without stock) and only warned about.
        self.assertEqual(body["valid_rows"], 2)
        self.assertEqual({e["row"] for e in body["errors"]}, {3})
        self.assertIn("سعر البيع", body["errors"][0]["message"])
        self.assertTrue(
            any("تكلفة/كمية" in w["message"] for w in body["warnings"])
        )
        self.assertEqual(models.Product.objects.unscoped().count(), 0)  # dry run writes nothing

    def test_duplicate_barcodes_and_extra_columns_are_preserved(self):
        """Within-file duplicate barcodes collapse to the most complete /
        highest-stock row (the one shown); the rest are kept in
        duplicated_products. Unrecognised columns are kept per-med in
        additional_metadata. Nothing in the file is silently dropped."""
        header = PRODUCT_HEADER + ["الموقع"]  # an extra, unmapped column
        r = self.post([
            header,
            ["1", "111", "Panadol Red", "10", "5", "3", "مسكنات", "رف A"],
            ["2", "111", "Panadol Blue", "12", "6", "20", "مسكنات", "رف B"],
            ["3", "222", "Solo", "7", "3", "1", "", ""],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        stats = r.json()["stats"]
        self.assertEqual(stats["created"], 2)           # one row per barcode
        self.assertEqual(stats["duplicates_kept"], 1)   # the extra 111 row
        self.assertEqual(stats["with_extra_columns"], 1)
        self.assertEqual(models.Product.objects.unscoped().count(), 2)

        med = models.Product.objects.unscoped().get(store=self.ph, barcode="111")
        self.assertEqual(med.name, "Panadol Blue")      # most stock wins
        self.assertEqual(med.stock, 20)
        self.assertEqual(med.additional_metadata, {"الموقع": "رف B"})
        self.assertEqual(len(med.duplicated_products), 1)
        dup = med.duplicated_products[0]
        self.assertEqual(dup["name"], "Panadol Red")
        self.assertEqual(dup["additional_metadata"], {"الموقع": "رف A"})

    def test_original_number_is_a_second_identity_code(self):
        """A product can carry BOTH باركود and الرقم الأصلي — both identify it.
        Both are stored so scanning either resolves it, and re-importing the
        product under the OTHER code updates the same med (no duplicate)."""
        header = ["اسم الصنف", "الباركود", "الرقم الأصلي", "سعر البيع"]
        r = self.post([
            header,
            ["Norvasc 10mg Tab - Pfizer", "7290013592415", "7298120002995", "19"],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(models.Product.objects.unscoped().count(), 1)
        med = models.Product.objects.unscoped().get(store=self.ph)
        self.assertEqual(med.barcode, "7290013592415")  # باركود is the primary
        self.assertEqual(med.original_number, "7298120002995")
        self.assertIn("7298120002995", med.alt_barcodes)  # → scannable via alt

        # Re-import the SAME product listed ONLY by its original number (in the
        # barcode column) → matches the existing med, does NOT create a second.
        r2 = self.post([
            ["اسم الصنف", "الباركود", "سعر البيع"],
            ["Norvasc 10mg Tab - Pfizer", "7298120002995", "21"],
        ], commit=True)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(models.Product.objects.unscoped().count(), 1)  # no dup
        med.refresh_from_db()
        self.assertEqual(str(med.price), "21.00")  # the same row was updated
        # Both codes still resolve the product.
        codes = [med.barcode, *med.alt_barcodes]
        self.assertIn("7290013592415", codes)
        self.assertIn("7298120002995", codes)

    def test_original_number_stops_same_name_collapse(self):
        """Two DIFFERENT products that share a name but carry different codes
        must NOT merge — giving الرقم الأصلي its own identity code is what keeps
        them apart (guards the mass same-name collapse)."""
        header = ["اسم الصنف", "الباركود", "الرقم الأصلي", "سعر البيع"]
        r = self.post([
            header,
            ["حفاضات", "111", "", "10"],  # identified by barcode
            ["حفاضات", "", "222", "12"],  # identified by original number
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(models.Product.objects.unscoped().count(), 2)

    def test_row_numbers_from_a_new_export_never_steal_identities(self):
        """FIELD INCIDENT 2026-07-13: «الرقم» is the export's ROW NUMBER, not
        a stable id. Importing a re-sorted/re-filtered export must NEVER let
        row numbers rewrite existing products' names/barcodes ("I scan the
        barcode and it says the item isn't there"). Barcode is the identity."""
        # Export A: row 1 = X (barcode 111)
        self.post([PRODUCT_HEADER, ["1", "111", "X", "10", "", "5", ""]], commit=True)
        # Export B, sorted differently: row 1 is now a DIFFERENT product Y.
        r = self.post([
            PRODUCT_HEADER,
            ["1", "222", "Y", "8", "", "3", ""],
            ["2", "111", "X", "12", "", "6", ""],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stats"], {"created": 1, "updated": 1, "expiry_set": 0})
        x = models.Product.objects.unscoped().get(store=self.ph, barcode="111")
        self.assertEqual(x.name, "X")            # identity intact
        self.assertEqual(str(x.price), "12.00")  # data updated from its own row
        y = models.Product.objects.unscoped().get(store=self.ph, barcode="222")
        self.assertEqual(y.name, "Y")
        # Nothing lost, nothing stolen: both barcodes scan.
        self.assertEqual(
            models.Product.objects.unscoped().filter(store=self.ph).count(), 2
        )

    def test_dry_run_warns_when_a_file_would_rename_existing_products(self):
        """Wrong store's file (same barcodes, different names) must scream
        in the preview before anyone commits."""
        self.post([PRODUCT_HEADER, ["1", "111", "منتجنا", "10", "", "", ""]], commit=True)
        r = self.post([PRODUCT_HEADER, ["1", "111", "منتج صيدلية أخرى", "9", "", "", ""]])
        body = r.json()
        self.assertFalse(body["committed"])
        self.assertEqual(body["renames"], 1)
        self.assertTrue(any("سيتغيّر اسمه" in w["message"] for w in body["warnings"]))

    def test_overflow_values_degrade_softly_instead_of_500(self):
        """A barcode pasted into a numeric cell (13 digits ≈ trillions) must
        never crash the atomic batch with Postgres 'numeric field overflow' —
        the item imports without that field. Field incident 2026-07-12."""
        r = self.post([
            PRODUCT_HEADER,
            # price = a pasted barcode; cost + qty also absurd
            ["1", "111", "Overflow", "6251581065016", "6251581065016",
             "6251581065016", ""],
            ["2", "222", "Fine", "10", "5", "3", ""],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["committed"])
        self.assertEqual(body["stats"]["created"], 2)
        med = models.Product.objects.unscoped().get(store=self.ph, barcode="111")
        self.assertEqual(str(med.price), "0.00")  # imported without the junk
        self.assertEqual(str(med.cost), "0.00")
        self.assertEqual(med.stock, 0)
        self.assertTrue(any("خارج" in w["message"] for w in body["warnings"]))

    def test_bad_cost_or_stock_never_blocks_the_import(self):
        r = self.post([
            PRODUCT_HEADER,
            ["1", "111", "Good", "10", "غير معروف", "kk", ""],  # junk cost + qty
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["committed"])
        med = models.Product.objects.unscoped().get(store=self.ph, barcode="111")
        self.assertEqual(str(med.price), "10.00")
        self.assertEqual(str(med.cost), "0.00")   # model default — not provided
        self.assertEqual(med.stock, 0)             # model default — not provided

    def test_commit_with_errors_is_refused_and_writes_nothing(self):
        r = self.post([
            PRODUCT_HEADER,
            ["1", "111", "Good", "10", "5", "3", ""],
            ["2", "222", "Bad", "xx", "5", "3", ""],
        ], commit=True)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(models.Product.objects.unscoped().count(), 0)  # atomic: all or nothing

    def test_commit_imports_and_is_upsert_by_source_then_barcode(self):
        r = self.post([
            PRODUCT_HEADER,
            ["1", "111", "Panadol", "10", "5", "3", "مسكنات"],
            ["2", "222", "Adol", "8", "4", "7", "مسكنات"],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stats"], {"created": 2, "updated": 0, "expiry_set": 0})
        med = models.Product.objects.unscoped().get(barcode="111")
        self.assertEqual(med.store_id, self.ph.pk)
        self.assertEqual(med.category.name, "مسكنات")
        # re-import with a new price → update, not duplicate
        r = self.post([PRODUCT_HEADER, ["1", "111", "Panadol Extra", "12", "6", "4", ""]], commit=True)
        self.assertEqual(r.json()["stats"], {"created": 0, "updated": 1, "expiry_set": 0})
        med.refresh_from_db()
        self.assertEqual((med.name, str(med.price), med.stock), ("Panadol Extra", "12.00", 4))
        self.assertEqual(models.Product.objects.unscoped().count(), 2)

    def test_import_never_touches_other_pharmacies(self):
        foreign = models.Product.objects.create(
            store=self.other, source_id="1", barcode="111",
            name="Foreign", price=Decimal("55.00"), stock=9,
        )
        self.post([PRODUCT_HEADER, ["1", "111", "Mine", "10", "5", "3", ""]], commit=True)
        foreign.refresh_from_db()
        self.assertEqual((foreign.name, str(foreign.price), foreign.stock), ("Foreign", "55.00", 9))
        self.assertEqual(models.Product.objects.unscoped().filter(store=self.ph).count(), 1)

    def test_wrong_file_is_a_clear_error(self):
        bad = BytesIO(b"not an excel file")
        bad.name = "x.xlsx"
        r = self.client_api.post("/api/v1/import/hesabate/products/", {"file": bad}, format="multipart")
        self.assertEqual(r.status_code, 400)
        self.assertIn("حساباتي", r.json()["detail"])

    def test_missing_columns_is_a_clear_error(self):
        r = self.post([["عمود غريب", "آخر"], ["1", "2"]])
        self.assertEqual(r.status_code, 400)
        self.assertIn("الأعمدة", r.json()["detail"])

    def test_only_name_barcode_price_columns_are_needed(self):
        r = self.post([
            ["الباركود", "اسم الصنف", "سعر البيع"],
            ["111", "Empty price", ""],
            ["222", "Zero price", "0"],
            ["333", "Negative price", "-5"],
            ["", "No barcode value", "12"],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["committed"])
        self.assertEqual(body["stats"]["created"], 4)
        self.assertEqual(models.Product.objects.unscoped().filter(store=self.ph).count(), 4)
        self.assertEqual(
            models.Product.objects.unscoped().get(store=self.ph, name="Empty price").price,
            Decimal("0.00"),
        )
        self.assertEqual(
            models.Product.objects.unscoped().get(
                store=self.ph, name="Negative price"
            ).price,
            Decimal("-5.00"),
        )

    def test_hesabate_price_list_export_shape(self):
        """The real 'قائمة الأسعار' export: price column is «مفرق» with values
        like «35 شيكل», numeric barcodes, «الرصيد الحالي» for stock."""
        header = [
            "الرقم", "الرقم الأصلي", "الاسم", "التكلفة", "باركود",
            "العلامة التجارية", "الشركة المنتجة", "الرصيد الحالي",
            "التصنيفات", "التصنيف", "الطراز", "اللون", "بونص",
            "ملاحظات", "باركود الوحدات", "مفرق",
        ]
        r = self.post([
            header,
            [1, None, "%LAMIRASE SPRAY 1", 20.44, 6251581065016,
             "بلا", "شركة بيت جالا", 8, None, "ادوية", None, None, None,
             None, None, "35 شيكل"],
            [2, None, "+fever scan", 0, 7290002193067.0,
             "بلا", "بلا", 1, None, "ادوية", None, None, None,
             None, None, "7.5 شيكل"],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        # Both rows carry columns the importer doesn't map (brand «العلامة
        # التجارية», الطراز/اللون, ملاحظات …) → preserved in additional_metadata.
        self.assertEqual(
            r.json()["stats"], {"created": 2, "updated": 0, "with_extra_columns": 2, "expiry_set": 0}
        )
        med = models.Product.objects.unscoped().get(store=self.ph, barcode="6251581065016")
        self.assertEqual(
            (str(med.price), str(med.cost), med.source_id),
            ("35.00", "20.44", "1"),
        )
        self.assertEqual(med.manufacturer.name, "شركة بيت جالا")
        self.assertEqual(med.category.name, "ادوية")
        # The unmapped brand column is kept rather than silently dropped.
        self.assertEqual(med.additional_metadata.get("العلامة التجارية"), "بلا")
        # float-typed barcode cell must not become "7290002193067.0"
        med2 = models.Product.objects.unscoped().get(store=self.ph, barcode="7290002193067")
        self.assertEqual(str(med2.price), "7.50")

    def test_multi_unit_price_cells_use_pack_price(self):
        header = ["الاسم", "باركود", "مفرق"]
        r = self.post([
            header,
            ["Augmentin", "111", "علبة 25 شيكل شريط 8.33 شيكل"],
            ["Strip only", "222", "شريط 2 شيكل"],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["stats"], {"created": 2, "updated": 0, "expiry_set": 0})
        self.assertIn("أسعار وحدات متعددة", body["warnings"][0]["message"])
        self.assertEqual(
            str(models.Product.objects.unscoped().get(store=self.ph, barcode="111").price),
            "25.00",
        )
        self.assertEqual(
            str(models.Product.objects.unscoped().get(store=self.ph, barcode="222").price),
            "2.00",
        )

    def test_multi_unit_dollar_price_is_not_imported_as_shekels(self):
        r = self.post([
            ["الاسم", "باركود", "مفرق"],
            ["Dollar item", "333", "علبة 25 دولار"],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("عملة أخرى", body["warnings"][0]["message"])
        med = models.Product.objects.unscoped().get(store=self.ph, barcode="333")
        self.assertEqual(str(med.price), "0.00")  # model default, NOT 25.00

    def test_multi_unit_price_with_thousands_separator(self):
        r = self.post([
            ["الاسم", "باركود", "مفرق"],
            ["Expensive", "444", "علبة 1,250 شيكل حبة 125 شيكل"],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            str(models.Product.objects.unscoped().get(store=self.ph, barcode="444").price),
            "1250.00",
        )

    def test_bulk_import_query_budget_at_scale(self):
        """20k rows must import in a bounded number of queries (bulk paths),
        not O(rows) — that's the difference between seconds and 30 minutes
        against a remote database."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from apps.store import importers

        n = 20_000
        rows = [
            {
                "row": i + 2,
                "name": f"Med {i}",
                "barcode": str(3000000000000 + i),
                "source_id": str(i + 1),
                "price": Decimal("10.00"),
                "cost": Decimal("5.00"),
                "stock": 3,
                "category": "ادوية" if i % 2 else "تجميل",
                "manufacturer": f"Company {i % 50}",
            }
            for i in range(n)
        ]
        with CaptureQueriesContext(connection) as ctx:
            stats = importers.import_products(self.ph, rows)
        self.assertEqual(stats, {"created": n, "updated": 0})
        # SQLite splits batches at its bind-variable cap; Postgres uses ~40 batches.
        self.assertLess(len(ctx.captured_queries), 800, f"{len(ctx.captured_queries)} queries")
        self.assertEqual(models.Product.objects.unscoped().filter(store=self.ph).count(), n)
        # re-import = pure update, still bounded
        with CaptureQueriesContext(connection) as ctx:
            stats = importers.import_products(self.ph, rows)
        self.assertEqual(stats, {"created": 0, "updated": n})
        # SQLite splits batches at its bind-variable cap; Postgres uses ~40 batches.
        self.assertLess(len(ctx.captured_queries), 800, f"{len(ctx.captured_queries)} queries")
        self.assertEqual(models.Product.objects.unscoped().filter(store=self.ph).count(), n)

    def test_bulk_import_never_creates_or_links_shared_catalog(self):
        """Tenant isolation: an import writes ONLY this store's rows.
        Legacy CatalogItem rows stay untouched and unlinked; no new ones appear."""
        legacy = models.CatalogItem.objects.create(barcode="111", name="Canonical")
        r = self.post([
            ["اسم الصنف", "الباركود", "سعر البيع"],
            ["My Panadol", "111", "10"],
            ["Brand New", "222", "8"],
            ["No Barcode Item", "", "5"],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        med = models.Product.objects.unscoped().get(store=self.ph, barcode="111")
        self.assertIsNone(med.catalog_item_id)  # never linked
        legacy.refresh_from_db()
        self.assertEqual(legacy.name, "Canonical")  # untouched
        self.assertFalse(models.CatalogItem.objects.filter(barcode="222").exists())
        self.assertEqual(models.CatalogItem.objects.count(), 1)  # only the legacy row
        self.assertIsNone(
            models.Product.objects.unscoped().get(store=self.ph, name="No Barcode Item").catalog_item_id
        )

    def test_reimport_without_barcode_keeps_legacy_product_link(self):
        """Existing rows may still carry a legacy shared-catalog link (until
        Phase C drops the column). Imports must leave it exactly as-is."""
        legacy = models.CatalogItem.objects.create(barcode="111", name="Panadol")
        med = models.Product.objects.create(
            store=self.ph, name="Panadol", barcode="111",
            price=Decimal("10.00"), catalog_item=legacy,
        )
        # same item re-imported by name only (no barcode column value)
        r = self.post([
            ["اسم الصنف", "الباركود", "سعر البيع"],
            ["Panadol", "", "12"],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        med.refresh_from_db()
        self.assertEqual(med.catalog_item_id, legacy.pk)  # legacy link untouched
        self.assertEqual(str(med.price), "12.00")

    def test_same_name_different_barcode_stays_distinct(self):
        """Distinct products often share a name (sizes/variants). A row with
        its OWN barcode must never merge into an earlier row by name."""
        r = self.post([
            ["اسم الصنف", "الباركود", "سعر البيع"],
            ["Panadol", "111", "10"],
            ["Panadol", "222", "18"],   # same name, different barcode → distinct
            ["Panadol", "", "12"],      # no barcode → merges by name (updates)
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stats"], {"created": 2, "updated": 1, "expiry_set": 0})
        meds = models.Product.objects.unscoped().filter(store=self.ph, name="Panadol")
        self.assertEqual(meds.count(), 2)
        self.assertEqual(
            sorted(m.barcode for m in meds), ["111", "222"]
        )
        # the barcode-less row updated the most recent same-name med
        self.assertEqual(
            str(models.Product.objects.unscoped().get(store=self.ph, barcode="222").price),
            "12.00",
        )
        # re-import: both survive, nothing new created
        r = self.post([
            ["اسم الصنف", "الباركود", "سعر البيع"],
            ["Panadol", "111", "10"],
            ["Panadol", "222", "18"],
        ], commit=True)
        self.assertEqual(r.json()["stats"], {"created": 0, "updated": 2, "expiry_set": 0})
        self.assertEqual(
            models.Product.objects.unscoped().filter(store=self.ph, name="Panadol").count(), 2
        )

    def test_unit_barcodes_become_alt_barcodes(self):
        """Hesabate's «باركود الوحدات» codes are scanned at their POS too —
        they must import as alt_barcodes so scanning them here finds the
        product (same stock, same price)."""
        header = ["الاسم", "باركود", "مفرق", "باركود الوحدات"]
        r = self.post([
            header,
            ["Nivea Roll", "4005900554437", "12 شيكل", ": 4005900088031\n: 4005900088062\n"],
            ["Johnson Oil", "3574669909150", "9 شيكل", ": 3574669909150\n"],  # repeats primary → ignored
            ["Plain", "111", "5", ""],
        ], commit=True)
        self.assertEqual(r.status_code, 200)
        med = models.Product.objects.unscoped().get(store=self.ph, barcode="4005900554437")
        self.assertEqual(med.alt_barcodes, ["4005900088031", "4005900088062"])
        self.assertEqual(
            models.Product.objects.unscoped().get(store=self.ph, barcode="3574669909150").alt_barcodes,
            [],
        )
        self.assertEqual(
            models.Product.objects.unscoped().get(store=self.ph, barcode="111").alt_barcodes,
            [],
        )
        # reimport: idempotent, no duplication
        r = self.post([
            header,
            ["Nivea Roll", "4005900554437", "12 شيكل", ": 4005900088031\n: 4005900088062\n"],
        ], commit=True)
        self.assertEqual(r.json()["stats"], {"created": 0, "updated": 1, "expiry_set": 0})
        med.refresh_from_db()
        self.assertEqual(med.alt_barcodes, ["4005900088031", "4005900088062"])
        # pos_catalog carries them for instant client-side scanning
        cat = self.client_api.get("/api/v1/products/pos_catalog/").json()
        row = next(x for x in cat["results"] if x["barcode"] == "4005900554437")
        self.assertEqual(row["alt_barcodes"], ["4005900088031", "4005900088062"])

    def test_catalog_version_changes_on_edit(self):
        from django.core.cache import cache as djcache
        self.post([["الاسم", "باركود", "مفرق"], ["Med", "111", "10"]], commit=True)
        v1 = self.client_api.get("/api/v1/products/catalog_version/").json()["version"]
        med = models.Product.objects.unscoped().get(store=self.ph, barcode="111")
        med.price = 12
        med.save()
        djcache.clear()  # simulate the write-path invalidation hook
        v2 = self.client_api.get("/api/v1/products/catalog_version/").json()["version"]
        self.assertNotEqual(v1, v2)

    def test_price_column_is_required(self):
        r = self.post([["الباركود", "اسم الصنف"], ["111", "No price col"]])
        self.assertEqual(r.status_code, 400)
        self.assertIn("السعر", r.json()["detail"])

    def test_barcode_column_is_required(self):
        r = self.post([["اسم الصنف", "سعر البيع"], ["No barcode col", "10"]])
        self.assertEqual(r.status_code, 400)
        self.assertIn("الباركود", r.json()["detail"])


INV_HEADER = ["رقم الفاتورة", "التاريخ", "الزبون", "المبلغ", "الخصم", "الدفع"]
ITEM_HEADER = ["رقم الفاتورة", "اسم الصنف", "الكمية", "السعر", "المجموع"]


class SalesImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph = models.Store.objects.create(name="A", slug="a")
        cls.user = User.objects.create_user("staff", password="x", store=cls.ph)
        cls.med = models.Product.objects.create(
            store=cls.ph, name="Panadol", price=Decimal("10.00"), stock=5
        )

    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)

    def post(self, invoices, items, commit=False, barcode=None):
        url = "/api/v1/import/hesabate/sales/" + ("?commit=true" if commit else "")
        data = {"invoices": xlsx(invoices), "items": xlsx(items)}
        if barcode is not None:
            data["barcode"] = barcode
        return self.client_api.post(url, data, format="multipart")

    def test_import_is_idempotent_backdated_and_stock_neutral(self):
        invoices = [INV_HEADER, ["77", "2024-03-05 14:30:00", "زبون", "20", "0", "نقدي"]]
        items = [ITEM_HEADER, ["77", "Panadol", "2", "10", "20"]]
        r = self.post(invoices, items, commit=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stats"]["created_sales"], 1)
        self.assertEqual(r.json()["stats"]["matched_items"], 1)
        sale = models.Sale.objects.unscoped().get(note="Hesabate #77")
        self.assertEqual(sale.store_id, self.ph.pk)
        self.assertEqual(sale.created_at.year, 2024)  # back-dated to the real moment
        self.med.refresh_from_db()
        self.assertEqual(self.med.stock, 5)  # history never moves stock
        # same file again → skipped, nothing duplicated
        r = self.post(invoices, items, commit=True)
        self.assertEqual(r.json()["stats"], {
            "created_sales": 0, "created_items": 0, "skipped_existing": 1,
            "matched_items": 0, "matched_by_barcode": 0, "unmatched_items": 0,
            "expiry_set": 0,
        })
        self.assertEqual(models.Sale.objects.unscoped().count(), 1)

    def test_bad_rows_are_skipped_and_flagged_not_blocking(self):
        # A broken row must NOT block the whole import: the good invoice goes in
        # and the bad-date row is skipped and flagged as a warning (not a hard
        # error that stops everything).
        invoices = [
            INV_HEADER,
            ["1", "2024-01-01", "x", "10", "0", "نقدي"],
            ["2", "not a date", "x", "10", "0", "نقدي"],
        ]
        items = [ITEM_HEADER, ["1", "Panadol", "1", "10", "10"]]
        r = self.post(invoices, items, commit=True)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["stats"]["created_sales"], 1)  # good one imported
        self.assertEqual(models.Sale.objects.unscoped().count(), 1)
        # the bad row (Excel row 3) is reported as a skipped warning, not an error
        self.assertIn(3, {w["row"] for w in r.json()["warnings"]})

    def test_unmatched_product_names_import_as_free_text_with_warning_count(self):
        invoices = [INV_HEADER, ["9", "2024-01-01", "x", "5", "0", "ذمم"]]
        items = [ITEM_HEADER, ["9", "منتج غير معروف", "1", "5", "5"]]
        r = self.post(invoices, items, commit=True)
        self.assertEqual(r.json()["stats"]["unmatched_items"], 1)
        item = models.SaleItem.objects.unscoped().get()
        self.assertIsNone(item.product_id)
        self.assertEqual(item.medication_name, "منتج غير معروف")
        self.assertEqual(models.Sale.objects.unscoped().get().payment_method, "debt")

    def test_real_hesabate_pos_export_headers_are_recognised(self):
        # The exact column names Hesabate's POS "حركة الأصناف" / "كشف فواتير"
        # exports use — invoice = الفاتورة, name = اسم الصنف, qty = عدد (bare, no
        # «ال»). This is the file that was being rejected.
        inv_header = [
            "تفاصيل", "الرقم", "التاريخ", "الاسم", "المبلغ", "الخدمة",
            "الخصم", "الخصم آلي", "الحالة", "الطاولات", "الصندوق",
            "نوع المبيعات", "نوع الدفع",
        ]
        item_header = [
            "#", "الفاتورة", "التاريخ", "الوقت", "اسم الصنف", "عدد",
            "السعر", "الخصم", "بونص", "المجموع",
        ]
        invoices = [
            inv_header,
            ["", "46016", "2026-01-01 18:02:08", "نقدي", "20", "0",
             "0", "0", "نقدي => محول", "", "الرئيسي", "", "نقدي"],
        ]
        items = [
            item_header,
            ["1", "46016", "2026-01-01 02:00:23", "18:02:08", "Panadol",
             "2", "10", "0", "0", "20"],
        ]
        r = self.post(invoices, items, commit=True)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["stats"]["created_sales"], 1)
        self.assertEqual(r.json()["stats"]["matched_items"], 1)

    # --- barcode enrichment (optional 3rd file: the item-movement report) -----

    def test_barcode_report_links_sale_lines_by_barcode_not_name(self):
        # The sale line name ("ACAMOL TABLETS") does NOT match the catalogue name
        # ("Acamol Teva") — only the barcode report can bridge them.
        med = models.Product.objects.create(
            store=self.ph, name="Acamol Teva", barcode="7290000800028",
            price=Decimal("15.00"), stock=3,
        )
        invoices = [INV_HEADER, ["50", "2024-05-05", "x", "15", "0", "نقدي"]]
        items = [ITEM_HEADER, ["50", "ACAMOL TABLETS", "1", "15", "15"]]
        barcode = html_xls([BARCODE_HEADER, barcode_row("ACAMOL TABLETS", "7290000800028")])
        r = self.post(invoices, items, commit=True, barcode=barcode)
        self.assertEqual(r.status_code, 200, r.content)
        stats = r.json()["stats"]
        self.assertEqual(stats["matched_items"], 1)
        self.assertEqual(stats["matched_by_barcode"], 1)
        item = models.SaleItem.objects.unscoped().get(medication_name="ACAMOL TABLETS")
        self.assertEqual(item.product_id, med.id)

    def test_barcode_matches_via_alt_barcodes(self):
        med = models.Product.objects.create(
            store=self.ph, name="Nexium", barcode="111",
            alt_barcodes=["5000456060455"], price=Decimal("40.00"), stock=2,
        )
        invoices = [INV_HEADER, ["51", "2024-05-06", "x", "40", "0", "نقدي"]]
        items = [ITEM_HEADER, ["51", "nexium 40mg 28 tab", "1", "40", "40"]]
        barcode = html_xls([BARCODE_HEADER, barcode_row("nexium 40mg 28 tab", "5000456060455")])
        r = self.post(invoices, items, commit=True, barcode=barcode)
        self.assertEqual(r.json()["stats"]["matched_by_barcode"], 1)
        self.assertEqual(
            models.SaleItem.objects.unscoped().get(
                medication_name="nexium 40mg 28 tab"
            ).product_id,
            med.id,
        )

    def test_ambiguous_barcode_name_falls_back_to_name_match(self):
        # Same item name → two different barcodes in the report: never force a
        # barcode; fall back to the name match and warn.
        invoices = [INV_HEADER, ["52", "2024-05-07", "x", "10", "0", "نقدي"]]
        items = [ITEM_HEADER, ["52", "Panadol", "1", "10", "10"]]
        barcode = html_xls([
            BARCODE_HEADER,
            barcode_row("Panadol", "111111"),
            barcode_row("Panadol", "222222"),
        ])
        r = self.post(invoices, items, commit=True, barcode=barcode)
        stats = r.json()["stats"]
        self.assertEqual(stats["matched_items"], 1)       # matched by NAME
        self.assertEqual(stats["matched_by_barcode"], 0)  # never by a guessed barcode
        self.assertEqual(
            models.SaleItem.objects.unscoped().get().product_id, self.med.id
        )
        self.assertTrue(
            any("أكثر من باركود" in w["message"] for w in r.json()["warnings"])
        )

    def test_barcode_map_targets_item_name_column_not_decoys(self):
        # البيان (blank) and الاسم («بيع نقدي») sit BEFORE اسم الصنف in the report;
        # the parser must key on اسم الصنف, never a decoy column.
        from apps.store import importers
        mapping, warnings = importers.parse_barcode_map(
            html_xls([BARCODE_HEADER, barcode_row("ACAMOL TABLETS", "7290000800028")])
        )
        self.assertEqual(mapping, {"acamol tablets": "7290000800028"})
        self.assertEqual(warnings, [])

    def test_html_xls_barcode_report_is_read_where_openpyxl_cannot(self):
        # The report is an HTML table with a .xls name — assert the importer's
        # HTML path reads it (openpyxl would raise on these bytes).
        from apps.store import importers
        mapping, _ = importers.parse_barcode_map(
            html_xls([
                BARCODE_HEADER,
                barcode_row("item one", "6251581010573"),
                barcode_row("item two", "12345"),      # <6 digits → ignored
            ])
        )
        self.assertEqual(mapping, {"item one": "6251581010573"})

    def test_dry_run_reports_barcode_pairs_count(self):
        invoices = [INV_HEADER, ["60", "2024-05-08", "x", "15", "0", "نقدي"]]
        items = [ITEM_HEADER, ["60", "ACAMOL TABLETS", "1", "15", "15"]]
        barcode = html_xls([BARCODE_HEADER, barcode_row("ACAMOL TABLETS", "7290000800028")])
        r = self.post(invoices, items, commit=False, barcode=barcode)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["committed"])
        self.assertEqual(r.json()["barcode_pairs"], 1)

    # --- expiry filled from the movement file's تاريخ الصلاحية column ---------

    def test_expiry_imported_from_barcode_file(self):
        med = models.Product.objects.create(
            store=self.ph, name="Acamol", barcode="7290000800028",
            price=Decimal("15"), stock=3,
        )
        invoices = [INV_HEADER, ["70", "2024-05-05", "x", "15", "0", "نقدي"]]
        items = [ITEM_HEADER, ["70", "Acamol", "1", "15", "15"]]
        barcode = html_xls([BARCODE_HEADER, barcode_row("Acamol", "7290000800028", "2027-02-01")])
        r = self.post(invoices, items, commit=True, barcode=barcode)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["stats"]["expiry_set"], 1)
        med.refresh_from_db()
        self.assertEqual(str(med.expiry_date), "2027-02-01")

    def test_expiry_3000_sentinel_is_ignored(self):
        med = models.Product.objects.create(
            store=self.ph, name="Cream", barcode="7290000800029",
            price=Decimal("5"), stock=2,
        )
        invoices = [INV_HEADER, ["71", "2024-05-05", "x", "5", "0", "نقدي"]]
        items = [ITEM_HEADER, ["71", "Cream", "1", "5", "5"]]
        barcode = html_xls([BARCODE_HEADER, barcode_row("Cream", "7290000800029", "3000-01-01")])
        r = self.post(invoices, items, commit=True, barcode=barcode)
        self.assertEqual(r.json()["stats"]["expiry_set"], 0)
        med.refresh_from_db()
        self.assertIsNone(med.expiry_date)

    def test_expiry_never_overwrites_an_existing_date(self):
        from datetime import date

        med = models.Product.objects.create(
            store=self.ph, name="Set", barcode="7290000800030",
            price=Decimal("5"), stock=2, expiry_date=date(2025, 1, 1),
        )
        invoices = [INV_HEADER, ["72", "2024-05-05", "x", "5", "0", "نقدي"]]
        items = [ITEM_HEADER, ["72", "Set", "1", "5", "5"]]
        barcode = html_xls([BARCODE_HEADER, barcode_row("Set", "7290000800030", "2028-01-01")])
        r = self.post(invoices, items, commit=True, barcode=barcode)
        self.assertEqual(r.json()["stats"]["expiry_set"], 0)  # already set → left alone
        med.refresh_from_db()
        self.assertEqual(str(med.expiry_date), "2025-01-01")

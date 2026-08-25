"""Hesabate → Pharma ingestion.

Parses the xlsx exports a store can produce from Hesabate's own reports
UI (no credentials needed), validates EVERY row with exact line numbers, and
imports atomically — either the whole file lands or nothing does.

Everything here is tenant-scoped: matching, creation, and updates only ever
touch the importing user's store.
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from io import BytesIO

from django.db import transaction
from django.utils import timezone

from . import models

TWO = Decimal("0.01")

#: Column capacities — Medication.price/cost are Decimal(12,2), stock is
#: Decimal(12,3). Anything beyond is a paste error (usually a barcode sitting
#: in a numeric cell), never a real store value: Postgres would reject the
#: whole atomic batch with 'numeric field overflow', so we degrade softly at
#: parse time instead (import the item without that field + one warning).
MAX_MONEY = Decimal("9999999999.99")
MAX_QTY = Decimal("999999999.999")

# Header synonyms (lowercased, stripped) → canonical field.
PRODUCT_HEADERS = {
    "source_id": {"رقم الصنف", "رقم المادة", "رقم", "code", "item code", "id", "الرقم"},
    "barcode": {"باركود", "الباركود", "barcode", "بار كود"},
    # «الرقم الأصلي» — a second identity code (may be the one on the box). Note:
    # it is distinct from «الرقم» above (that is a row number), so no collision.
    "original_number": {
        "الرقم الأصلي", "الرقم الاصلي", "رقم أصلي", "رقم اصلي",
        "الأصلي", "الاصلي", "original", "original number", "original_number",
    },
    "name": {"اسم الصنف", "الصنف", "اسم المادة", "المادة", "الاسم", "name", "item name", "البيان"},
    "price": {"سعر البيع", "السعر", "سعر بيع", "price", "بيع", "سعر المبيع", "مفرق", "سعر المفرق", "سعر مفرق"},
    "cost": {"الكلفة", "التكلفة", "كلفة", "cost", "سعر الكلفة", "سعر الشراء", "شراء"},
    "stock": {"الرصيد", "رصيد", "الكمية", "كمية", "qty", "quantity", "stock", "المخزون", "الرصيد الحالي"},
    "category": {"التصنيف", "تصنيف", "category", "المجموعة", "مجموعة", "الفئة"},
    "manufacturer": {"الشركة", "الشركة المنتجة", "المنتج", "manufacturer", "company", "المورد"},
    # Hesabate's unit/packaging barcodes — the POS there scans these too, so
    # skipping them made in-store products look "missing from the file".
    "unit_barcodes": {"باركود الوحدات", "باركودات الوحدات", "unit barcodes"},
}

INVOICE_HEADERS = {
    "id": {"رقم الفاتورة", "رقم", "الرقم", "id", "invoice", "فاتورة"},
    "dt": {"التاريخ", "تاريخ", "date", "التاريخ والوقت", "الوقت"},
    "customer": {"الزبون", "العميل", "اسم الزبون", "customer", "الاسم"},
    "amount": {"المبلغ", "الاجمالي", "الإجمالي", "الصافي", "amount", "total", "المجموع"},
    "discount": {"الخصم", "خصم", "discount"},
    "payment": {"الدفع", "نوع الدفع", "طريقة الدفع", "payment", "نقدي/ذمم", "النوع"},
}

ITEM_HEADERS = {
    "invoice_id": {"رقم الفاتورة", "الفاتورة", "invoice", "invoice id", "رقم"},
    "name": {"اسم الصنف", "الصنف", "المادة", "الاسم", "name", "البيان"},
    "qty": {"الكمية", "كمية", "qty", "quantity", "العدد", "عدد"},
    "price": {"السعر", "سعر البيع", "سعر الوحدة", "price", "الافرادي"},
    "total": {"المجموع", "الاجمالي", "الإجمالي", "total", "المبلغ"},
}


FIELD_LABELS = {
    "name": "الاسم",
    "barcode": "الباركود",
    "price": "السعر",
    "cost": "الكلفة",
    "stock": "الكمية",
    "qty": "الكمية",
    "invoice_id": "رقم الفاتورة",
    "id": "رقم الفاتورة",
    "amount": "المبلغ",
}


class ImportProblem(Exception):
    """A file-level problem that stops the import (bad file, bad headers)."""


ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# Currency words Hesabate mixes into money cells (e.g. "35 شيكل", "0NIS").
CURRENCY_WORDS = ("شيكل", "شيقل", "شواقل", "ش.ج", "nis", "ils", "₪")


def norm(value) -> str:
    return " ".join(str(value if value is not None else "").split())


def _norm_code(value) -> str:
    """Barcodes/IDs: Excel often stores these as numbers — 111.0 → "111"."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return norm(value).translate(ARABIC_DIGITS)


_UNIT_BC_RE = re.compile(r"\d{4,20}")


def _unit_barcodes(value, primary: str) -> list[str]:
    """Extract the scannable digit codes out of a «باركود الوحدات» cell.

    Hesabate writes them as ": 4005900088031\\n" lines, possibly several per
    cell. Order-preserving, de-duplicated, never repeating the primary."""
    if value in (None, ""):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for code in _UNIT_BC_RE.findall(str(value).translate(ARABIC_DIGITS)):
        if code != primary and code not in seen:
            seen.add(code)
            out.append(code)
    return out[:10]


def _to_decimal(value, default=None):
    if isinstance(value, bool):
        raise ValueError(str(value))
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(TWO)
    text = norm(value).translate(ARABIC_DIGITS).replace(",", "").replace("٬", "")
    lowered = text.lower()
    for word in CURRENCY_WORDS:
        lowered = lowered.replace(word, "")
    text = lowered.strip()
    if text == "":
        return default
    try:
        return Decimal(text).quantize(TWO)
    except InvalidOperation as exc:
        raise ValueError(norm(value)) from exc


# Unit words Hesabate uses in multi-unit price cells: "علبة 25 شيكل شريط 8.33 شيكل".
UNIT_WORDS = (
    "علبة", "عبوة", "شريط", "حبة", "حبه", "قطعة", "كبسولة", "كيس", "باكيت",
    "امبولة", "أمبولة", "امبول", "أمبول", "ابرة", "إبرة", "ربطة", "ربطه",
)
_NUM_RE = r"(-?\d+(?:\.\d+)?)"


def _price_from_cell(value):
    """→ (Decimal|None, is_multi_unit). Raises ValueError on garbage.

    Plain numbers (incl. "35 شيكل") go through _to_decimal. Cells that list
    several unit prices («علبة 25 شيكل شريط 8.33 شيكل») resolve to the pack
    (علبة/عبوة) price, or the first number if no pack label exists.
    """
    try:
        return _to_decimal(value, default=None), False
    except ValueError:
        pass
    text = norm(value).translate(ARABIC_DIGITS).replace(",", "").replace("٬", "")
    if "دولار" in text or "$" in text or "usd" in text.lower():
        # foreign currency — let the caller's warning path handle it
        raise ValueError(norm(value))
    if not any(word in text for word in UNIT_WORDS):
        raise ValueError(norm(value))
    for word in ("علبة", "عبوة"):
        m = re.search(word + r"\s*" + _NUM_RE, text)
        if m:
            return Decimal(m.group(1)).quantize(TWO), True
    m = re.search(_NUM_RE, text)
    if m:
        return Decimal(m.group(1)).quantize(TWO), True
    raise ValueError(norm(value))


def _to_int(value, default=None):
    d = _to_decimal(value, default=None)
    if d is None:
        return default
    return int(d)


class _TableRows(HTMLParser):
    """Collect every <table>'s rows as lists of cell strings (handles colspan).

    Hesabate's item-movement report — the ONE export that carries barcodes — is
    an HTML table saved with an .xls name, not a real workbook, so openpyxl
    can't read it. This turns it into the same (row → cells) shape.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._stack = []
        self._row = None
        self._cell = None
        self._pending = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._pending = 1
            for key, val in attrs:
                if key == "colspan":
                    try:
                        self._pending = max(1, int(val))
                    except (TypeError, ValueError):
                        self._pending = 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            for _ in range(self._pending - 1):
                self._row.append("")  # widen a colspan into blank columns
            self._cell = None
            self._pending = 0
        elif tag == "tr" and self._row is not None:
            if self._stack:
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())


def _load_html_rows(data: bytes):
    """Yield (row_number, [cells…]) from the LARGEST table in an HTML .xls."""
    parser = _TableRows()
    try:
        parser.feed(data.decode("utf-8", "replace"))
        parser.close()
    except Exception as exc:  # noqa: BLE001
        raise ImportProblem(
            "تعذر قراءة الملف — تأكد أنه ملف مُصدَّر من حساباتي."
        ) from exc
    if not parser.tables:
        raise ImportProblem(
            "لم أجد جدولاً في هذا الملف — تأكد أنه تقرير مُصدَّر من حساباتي."
        )
    best = max(parser.tables, key=lambda t: sum(len(r) for r in t))
    for idx, row in enumerate(best, start=1):
        yield idx, list(row)


def _load_rows(uploaded_file):
    """Yield (row_number, [cell values…]) from an xlsx OR an html-.xls upload.

    Hesabate exports two shapes: a real .xlsx (a ZIP, magic ``PK``) from most
    reports, and an HTML table saved as .xls from the item-movement report (the
    only one with barcodes). We read the bytes once and dispatch on the magic
    number, so either lands through the same downstream parser.
    """
    data = uploaded_file.read()
    if data[:2] == b"PK":  # .xlsx is a ZIP archive
        try:
            from openpyxl import load_workbook

            wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001
            raise ImportProblem(
                "تعذر قراءة الملف — تأكد أنه ملف Excel ‏(xlsx) مُصدَّر من حساباتي."
            ) from exc
        ws = wb.worksheets[0]
        # No row cap — a store may import its entire catalogue in one file.
        for idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            yield idx, list(row or [])
    else:  # HTML table saved as .xls (item-movement / barcode report)
        yield from _load_html_rows(data)


def _map_headers(cells, header_map):
    """Try to interpret a row as the header row. Returns {field: col_index}."""
    found = {}
    for col, cell in enumerate(cells):
        text = norm(cell).lower()
        if not text:
            continue
        for field, synonyms in header_map.items():
            if field not in found and text in synonyms:
                found[field] = col
    return found


def _parse_sheet(uploaded_file, header_map, required, label):
    """Shared parse loop → (columns, data_rows, header_cells).

    `header_cells` is the raw header row, so a caller can recover the labels of
    columns that are NOT in `header_map` (used to preserve unmapped columns as
    additional_metadata). Raises ImportProblem.
    """
    rows = list(_load_rows(uploaded_file))
    columns = None
    header_row = 0
    header_cells = []
    for idx, cells in rows[:10]:  # header must be near the top
        mapped = _map_headers(cells, header_map)
        if all(field in mapped for field in required):
            columns, header_row, header_cells = mapped, idx, cells
            break
    if columns is None:
        raise ImportProblem(
            f"لم أتعرف على أعمدة {label} في الملف — يجب أن يحتوي على الأعمدة: "
            + "، ".join(FIELD_LABELS.get(f, f) for f in sorted(required))
            + ". يمكن أن تكون القيم فارغة، لكن يجب وجود العناوين."
        )
    data = [(idx, cells) for idx, cells in rows if idx > header_row]
    return columns, data, header_cells


def _cell(cells, columns, field):
    col = columns.get(field)
    if col is None or col >= len(cells):
        return None
    return cells[col]


# --- Products ----------------------------------------------------------------

def _json_safe(value):
    """Coerce a raw cell value into something a JSONField can store."""
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (int, float, str)):
        return value
    return norm(value)


def _row_codes(row) -> list[str]:
    """Every identity code a row carries — barcode, original number, and unit
    barcodes — de-duplicated, order preserved. Two rows/records are the SAME
    product when any of these codes overlap."""
    out: list[str] = []
    for c in [row.get("barcode"), row.get("original_number"), *(row.get("alt_barcodes") or [])]:
        if c and c not in out:
            out.append(c)
    return out


def _dup_entry(row):
    """A JSON-safe snapshot of one duplicate row, stashed on the primary med."""
    return {
        "row": row.get("row"),
        "name": row.get("name"),
        "barcode": row.get("barcode"),
        "original_number": row.get("original_number"),
        "source_id": row.get("source_id"),
        "price": _json_safe(row.get("price")),
        "cost": _json_safe(row.get("cost")),
        "stock": _json_safe(row.get("stock")),
        "category": row.get("category"),
        "manufacturer": row.get("manufacturer"),
        "alt_barcodes": row.get("alt_barcodes") or [],
        "additional_metadata": row.get("additional_metadata") or {},
    }


def _completeness(row):
    """Rank a row for 'most complete / most stock wins' on duplicate barcodes.

    Sorts primarily by how many fields are filled, then by stock — so the
    richest row for a barcode becomes the shown medication.
    """
    filled = sum(
        1
        for k in ("price", "cost", "category", "manufacturer", "source_id")
        if row.get(k) not in (None, "", [])
    )
    filled += 1 if row.get("alt_barcodes") else 0
    filled += len(row.get("additional_metadata") or {})
    stock = row.get("stock")
    stock = Decimal(str(stock)) if stock not in (None, "") else Decimal("0")
    return (filled, stock)


def parse_products(uploaded_file):
    """→ (rows, errors, warnings). Each error: {row, message}."""
    columns, data, header_cells = _parse_sheet(
        uploaded_file, PRODUCT_HEADERS, {"name", "barcode", "price"}, "المنتجات"
    )
    # Columns present in the file that we DON'T map to a known field — their
    # header label + value are preserved per row as additional_metadata so an
    # import never silently drops data the store put in the sheet.
    mapped_cols = set(columns.values())
    extra_cols = [
        (col, norm(header_cells[col]))
        for col in range(len(header_cells))
        if col not in mapped_cols and norm(header_cells[col])
    ]
    rows, errors, warnings = [], [], []
    multi_unit_rows = 0
    soft_value_rows = 0
    for idx, cells in data:
        raw = {f: _cell(cells, columns, f) for f in PRODUCT_HEADERS}
        name = norm(raw["name"])
        if not name:
            if any(norm(c) for c in cells):
                # a totals/footer line — skip quietly unless it has numbers only
                warnings.append({"row": idx, "message": "صف بدون اسم صنف — تم تجاهله"})
            continue
        row = {"row": idx, "name": name, "barcode": _norm_code(raw["barcode"])}
        ok = True
        try:
            row["price"], is_multi = _price_from_cell(raw["price"])
            multi_unit_rows += 1 if is_multi else 0
        except ValueError as exc:
            text = str(exc)
            if "دولار" in text or "$" in text or "usd" in text.lower():
                # foreign-currency price: import the item, leave price for manual entry
                row["price"] = None
                warnings.append({
                    "row": idx,
                    "message": f"سعر بعملة أخرى «{text}» — استُورد الصنف بدون سعر، حدّثه يدوياً",
                })
            else:
                errors.append({"row": idx, "message": f"سعر البيع ليس رقماً: «{text}»"})
                ok = False
        # Out-of-range price = a pasted barcode, not a price. Import the item
        # without it rather than crash Postgres mid-batch.
        if row.get("price") is not None and abs(row["price"]) > MAX_MONEY:
            row["price"] = None
            soft_value_rows += 1
        # Only the identity trio (name / barcode / price) can BLOCK an import.
        # cost and stock are wanted but never worth failing a 23k-row file
        # over: an unreadable or absurd value imports as "not provided" + one
        # summary warning, and the store fixes those items at leisure.
        try:
            row["cost"] = _to_decimal(raw["cost"], default=None)
            if row["cost"] is not None and abs(row["cost"]) > MAX_MONEY:
                row["cost"] = None
                soft_value_rows += 1
        except ValueError:
            row["cost"] = None
            soft_value_rows += 1
        try:
            row["stock"] = _to_int(raw["stock"], default=None)
            if row["stock"] is not None and abs(Decimal(row["stock"])) > MAX_QTY:
                row["stock"] = None
                soft_value_rows += 1
        except ValueError:
            row["stock"] = None
            soft_value_rows += 1
        row["source_id"] = _norm_code(raw["source_id"])
        row["original_number"] = _norm_code(raw.get("original_number"))
        row["category"] = norm(raw["category"])
        row["manufacturer"] = norm(raw["manufacturer"])
        row["alt_barcodes"] = _unit_barcodes(raw.get("unit_barcodes"), row["barcode"])
        # The product's full identity code set (barcode + original number + unit
        # barcodes). Any shared code = the same product.
        row["codes"] = _row_codes(row)
        # Preserve every column we don't recognise ({header: value}) so an
        # import never drops data the store typed into extra columns.
        meta = {}
        for col, hlabel in extra_cols:
            val = cells[col] if col < len(cells) else None
            if val is None or norm(val) == "":
                continue
            meta[hlabel] = _json_safe(val)
        row["additional_metadata"] = meta
        if ok:
            rows.append(row)

    # Collapse within-file duplicates by ANY shared identity code (barcode /
    # original number / unit barcode). The most complete / highest-stock row
    # becomes the shown medication; the others are preserved on it under
    # `duplicated_products`, and every code they carried is unioned onto the
    # primary so scanning any of them resolves this one product. Rows with NO
    # code at all are left as-is (name-matched at import time).
    collapsed, pos_by_code, dup_barcode_rows = [], {}, 0

    def _register(pos, codes):
        for c in codes:
            pos_by_code[c] = pos

    for row in rows:
        codes = row["codes"]
        hit = next((pos_by_code[c] for c in codes if c in pos_by_code), None)
        if hit is None:
            row.setdefault("duplicated_products", [])
            _register(len(collapsed), codes)
            collapsed.append(row)
            continue
        dup_barcode_rows += 1
        primary = collapsed[hit]
        union = primary["codes"] + [c for c in codes if c not in primary["codes"]]
        if _completeness(row) > _completeness(primary):
            # the newcomer is richer → it becomes the shown med, old one stashed
            row["duplicated_products"] = primary.pop("duplicated_products", [])
            row["duplicated_products"].append(_dup_entry(primary))
            row["codes"] = union
            collapsed[hit] = row
        else:
            primary["duplicated_products"].append(_dup_entry(row))
            primary["codes"] = union
        _register(hit, union)
    rows = collapsed
    meta_rows = sum(1 for r in rows if r.get("additional_metadata"))

    if soft_value_rows:
        warnings.insert(0, {
            "row": 0,
            "message": (
                f"{soft_value_rows} قيمة سعر/تكلفة/كمية غير مفهومة أو خارج "
                "النطاق — استُوردت الأصناف بدونها، حدّثها لاحقاً"
            ),
        })
    if meta_rows:
        labels = "، ".join(lbl for _, lbl in extra_cols)
        warnings.insert(0, {
            "row": 0,
            "message": (
                f"{meta_rows} صنف يحتوي أعمدة إضافية غير قياسية "
                f"({labels}) — حُفِظت ضمن «معلومات إضافية» (additional_metadata)"
            ),
        })
    if dup_barcode_rows:
        warnings.insert(0, {
            "row": 0,
            "message": (
                f"{dup_barcode_rows} سطر مكرر داخل الملف (نفس الباركود أو الرقم "
                "الأصلي) — عُرض الصنف الأكمل/الأكثر رصيداً وحُفظت بقية الأسطر في "
                "«المنتجات المكررة» (duplicated_products) للرجوع إليها لاحقاً"
            ),
        })
    if multi_unit_rows:
        warnings.insert(0, {
            "row": 0,
            "message": (
                f"{multi_unit_rows} صف يحتوي أسعار وحدات متعددة (علبة/شريط) — "
                "تم اعتماد سعر العلبة، ويمكن إضافة أسعار الوحدات كأنواع لاحقاً"
            ),
        })
    return rows, errors, warnings


def import_products(store, rows):
    """BULK upsert into ONE store's catalogue. Caller wraps in a transaction.

    Matching (always inside the store): by ANY identity CODE — barcode,
    original number («الرقم الأصلي») or unit barcode. Multiple codes point to
    the same product, so a row reconciles with an existing med if any code
    matches. Name is a last resort ONLY for rows with no code at all. Never
    source_id — it's the export's row number, not an identity.
    Only the fields present in the file are touched. Everything is batched —
    a 23k-row file costs ~60 queries instead of ~100k, which is the difference
    between seconds and half an hour against a remote database.
    """
    # ── 1. Prefetch this store's listings once ────────────────────────────
    # by_code maps EVERY code a med carries → the med, so a file row that
    # arrives under any one of the product's codes finds it.
    by_code, by_name = {}, {}
    # NOTE: this .only() list must cover every field bulk_update() writes at
    # the end, or deferred-field loading quietly reintroduces per-row queries.
    for m in models.Product.objects.for_pharmacy(store).only(
        "id", "source_id", "barcode", "original_number", "name", "price",
        "cost", "stock", "category_id", "manufacturer_id", "updated_at",
        "alt_barcodes", "additional_metadata", "duplicated_products",
    ):
        for c in [m.barcode, m.original_number, *(m.alt_barcodes or [])]:
            if c:
                by_code.setdefault(c, m)
        by_name.setdefault(norm(m.name).lower(), m)

    # ── 2. Taxonomies: fetch all, bulk-create the missing ───────────────────
    cat_names = {row["category"][:120] for row in rows if row["category"]}
    man_names = {row["manufacturer"][:255] for row in rows if row["manufacturer"]}
    cats = {c.name.lower(): c for c in models.Category.objects.for_pharmacy(store)}
    mans = {m.name.lower(): m for m in models.Manufacturer.objects.for_pharmacy(store)}

    def missing(names, existing, model):
        """One instance per case-insensitive name not already in the DB."""
        seen = {}
        for n in sorted(names):
            if n.lower() not in existing and n.lower() not in seen:
                seen[n.lower()] = model(store=store, name=n)
        return list(seen.values())

    models.Category.objects.bulk_create(
        missing(cat_names, cats, models.Category), batch_size=1000, ignore_conflicts=True
    )
    models.Manufacturer.objects.bulk_create(
        missing(man_names, mans, models.Manufacturer), batch_size=1000, ignore_conflicts=True
    )
    cats = {c.name.lower(): c for c in models.Category.objects.for_pharmacy(store)}
    mans = {m.name.lower(): m for m in models.Manufacturer.objects.for_pharmacy(store)}

    # ── 3. (removed) The shared Product catalog is RETIRED — tenant isolation.
    # An import never creates or links global rows anymore: each store's
    # catalogue is fully self-contained, keyed by (store, barcode). Legacy
    # product links on existing rows are left untouched until Phase C drops
    # the column.

    # ── 4. Upsert in memory, preserving file order (later rows win) ─────────
    now = timezone.now()

    def apply(med, row):
        med.name = row["name"]
        # A product's identity codes only ever ACCUMULATE: union the row's codes
        # with any the med already carries, so re-importing it under one code
        # never drops the others (multiple codes → one product).
        known = [med.barcode, med.original_number, *(med.alt_barcodes or [])]
        all_codes = []
        for c in [*known, *(row.get("codes") or _row_codes(row))]:
            if c and c not in all_codes:
                all_codes.append(c)
        # Primary scannable code: the file's barcode, else its original number,
        # else whatever the med already had. (.get so hand-built rows work too.)
        on = row.get("original_number") or ""
        primary = row.get("barcode") or on or med.barcode
        if primary:
            med.barcode = primary
        if on:
            med.original_number = on
        # Every OTHER code becomes an alt barcode, so scanning any of them — the
        # original number, unit barcodes, or codes from duplicate rows — resolves
        # this same product (the scan path already checks alt_barcodes).
        med.alt_barcodes = [c for c in all_codes if c != primary]
        if row["source_id"]:
            med.source_id = row["source_id"]
        if row["price"] is not None:
            med.price = row["price"]
        if row["cost"] is not None:
            med.cost = row["cost"]
        if row["stock"] is not None:
            med.stock = row["stock"]
        if row["category"]:
            med.category = cats[row["category"][:120].lower()]
        if row["manufacturer"]:
            med.manufacturer = mans[row["manufacturer"][:255].lower()]
        # Only write when the file actually carried extras, so a later plain
        # re-import never wipes a previously-saved stash.
        if row.get("additional_metadata"):
            med.additional_metadata = row["additional_metadata"]
        if row.get("duplicated_products"):
            med.duplicated_products = row["duplicated_products"]
        med.updated_at = now

    to_create, to_update = [], {}
    created = updated = 0
    for row in rows:
        # Match on ANY identity code (barcode / original number / unit barcode):
        # multiple codes point to the SAME product, so a row reconciles with an
        # existing med whenever any code matches.
        #
        # source_id is NEVER used for matching (field incident 2026-07-13):
        # Hesabate's «الرقم» is the ROW NUMBER of that export, not a stable id —
        # a re-sorted export renumbers everything, so serial matching rewrote
        # products' names AND barcodes with other products' data.
        #
        # Name is a LAST RESORT — used only for a row that carries no code at
        # all. Pharmacies legitimately sell distinct products under one name
        # (incident: 338 distinct items collapsed via name-merge), and giving
        # «الرقم الأصلي» its own code is exactly what keeps them apart now.
        codes = row.get("codes") or _row_codes(row)
        med = next((by_code[c] for c in codes if c in by_code), None)
        if med is None and not codes:
            med = by_name.get(row["name"].lower())
        if med is None:
            med = models.Product(store=store)
            apply(med, row)
            to_create.append(med)
            created += 1
        else:
            apply(med, row)
            if med.pk:
                to_update[med.pk] = med
            updated += 1
        # Index this med under every code it now carries, so later rows in the
        # same file reconcile against it.
        for c in [med.barcode, med.original_number, *(med.alt_barcodes or [])]:
            if c:
                by_code[c] = med
        by_name[norm(med.name).lower()] = med

    # ── 5. Write in batches ──────────────────────────────────────────────────
    models.Product.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        models.Product.objects.bulk_update(
            to_update.values(),
            [
                "name", "barcode", "original_number", "source_id", "price",
                "cost", "stock", "category", "manufacturer", "alt_barcodes",
                "additional_metadata", "duplicated_products", "updated_at",
            ],
            batch_size=500,
        )
    # Extra counters are added ONLY when non-zero, so a plain import keeps the
    # exact {created, updated} shape the API and its tests already rely on.
    stats = {"created": created, "updated": updated}
    dup_kept = sum(len(r.get("duplicated_products") or []) for r in rows)
    with_meta = sum(1 for r in rows if r.get("additional_metadata"))
    if dup_kept:
        stats["duplicates_kept"] = dup_kept
    if with_meta:
        stats["with_extra_columns"] = with_meta
    return stats


# --- Sales ---------------------------------------------------------------------

DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y",
)


def _parse_dt(value):
    if isinstance(value, datetime):
        return value
    text = norm(value)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    raise ValueError(text)


def parse_sales(invoices_file, items_file):
    """→ (invoices, items_by_invoice, errors, warnings)."""
    columns, data, _ = _parse_sheet(
        invoices_file, INVOICE_HEADERS, {"id", "dt", "amount"}, "الفواتير"
    )
    invoices, errors, warnings = [], [], []
    seen = set()
    for idx, cells in data:
        raw = {f: _cell(cells, columns, f) for f in INVOICE_HEADERS}
        ext_id = norm(raw["id"])
        if not ext_id:
            continue
        if ext_id in seen:
            warnings.append({"row": idx, "sheet": "الفواتير", "message": f"رقم فاتورة مكرر ({ext_id}) — تم تخطي السطر"})
            continue
        seen.add(ext_id)
        inv = {"row": idx, "id": ext_id}
        try:
            inv["dt"] = _parse_dt(raw["dt"])
        except ValueError as exc:
            warnings.append({"row": idx, "sheet": "الفواتير", "message": f"تاريخ غير مفهوم «{exc}» — تم تخطي الفاتورة"})
            continue
        try:
            inv["amount"] = _to_decimal(raw["amount"], default=Decimal("0.00"))
            inv["discount"] = _to_decimal(raw["discount"], default=Decimal("0.00"))
        except ValueError as exc:
            warnings.append({"row": idx, "sheet": "الفواتير", "message": f"مبلغ غير صالح «{exc}» — تم تخطي الفاتورة"})
            continue
        payment = norm(raw["payment"]).lower()
        inv["payment"] = "debt" if payment in {"ذمم", "دين", "debt", "آجل", "اجل"} else "cash"
        invoices.append(inv)

    icolumns, idata, _ = _parse_sheet(
        items_file, ITEM_HEADERS, {"invoice_id", "name", "qty"}, "أصناف الفواتير"
    )
    known = {inv["id"] for inv in invoices}
    items_by_invoice = {}
    orphans = 0
    for idx, cells in idata:
        raw = {f: _cell(cells, icolumns, f) for f in ITEM_HEADERS}
        inv_id = norm(raw["invoice_id"])
        name = norm(raw["name"])
        if not inv_id or not name:
            continue
        if inv_id not in known:
            orphans += 1
            continue
        try:
            qty = _to_int(raw["qty"], default=1) or 1
            price = _to_decimal(raw["price"], default=Decimal("0.00"))
        except ValueError as exc:
            warnings.append({"row": idx, "sheet": "الأصناف", "message": f"قيمة غير رقمية «{exc}» — تم تخطي الصنف"})
            continue
        items_by_invoice.setdefault(inv_id, []).append(
            {"row": idx, "name": name, "qty": qty, "price": price}
        )
    if orphans:
        warnings.append(
            {"row": 0, "sheet": "الأصناف", "message": f"{orphans} سطر صنف لفواتير غير موجودة في ملف الفواتير — تم تجاهلها"}
        )
    return invoices, items_by_invoice, errors, warnings


# The item-movement report repeats several name-ish headers (البيان is blank,
# الاسم is the customer «بيع نقدي»), so we target «اسم الصنف» specifically and
# never the generic name synonyms — matching the wrong column silently maps
# every barcode onto one empty name.
BARCODE_MAP_HEADERS = {
    "name": {"اسم الصنف", "الصنف", "اسم المادة", "المادة"},
    "barcode": {"باركود", "الباركود", "barcode", "بار كود"},
}


def parse_barcode_map(barcode_file):
    """Build {sale-item-name → barcode} from Hesabate's item-movement report.

    That report is the ONLY export pairing the free-text POS item name with the
    product barcode, so it lets years of imported sale lines link to the
    catalogue by barcode instead of by (mismatching) name. Only a name that maps
    to EXACTLY ONE ≥6-digit barcode is kept — an ambiguous name falls back to the
    existing name match, never a wrong barcode. → ({name→barcode}, warnings).
    """
    columns, data, _ = _parse_sheet(
        barcode_file, BARCODE_MAP_HEADERS, {"name", "barcode"}, "حركة الأصناف (الباركود)"
    )
    pairs = {}
    for _idx, cells in data:
        name = norm(_cell(cells, columns, "name")).lower()
        # _norm_code turns a numeric cell (Excel stores barcodes as 6251…0.0)
        # back into a clean digit string; ≥6 digits filters out Hesabate's tiny
        # internal codes ("6", "11") that would never match a real product.
        code = _norm_code(_cell(cells, columns, "barcode"))
        if not name or not re.fullmatch(r"\d{6,}", code):
            continue
        pairs.setdefault(name, set()).add(code)
    name_barcode = {n: next(iter(codes)) for n, codes in pairs.items() if len(codes) == 1}
    warnings = []
    ambiguous = sum(1 for codes in pairs.values() if len(codes) > 1)
    if ambiguous:
        warnings.append({
            "row": 0, "sheet": "الباركود",
            "message": (
                f"{ambiguous} اسم صنف يحمل أكثر من باركود — تمت مطابقتها بالاسم "
                "بدل الباركود"
            ),
        })
    return name_barcode, warnings


EXPIRY_MAP_HEADERS = {
    "barcode": {"باركود", "الباركود", "barcode", "بار كود"},
    "expiry": {"تاريخ الصلاحية", "الصلاحية", "تاريخ الانتهاء", "expiry", "expiry date"},
}

# Hesabate writes "no expiry" (cosmetics, consumables) as a far-future sentinel.
_EXPIRY_SENTINEL_YEAR = 3000


def _parse_expiry(value):
    """A real expiry date, or None (blank / unparseable / the 3000 sentinel)."""
    from datetime import date

    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        text = norm(value)[:10]
        d = None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                d = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            return None
    return None if d.year >= _EXPIRY_SENTINEL_YEAR else d


def parse_expiry_map(barcode_file):
    """Build {barcode: latest real expiry date} from the item-movement report.

    Same file as the barcode map — it carries «تاريخ الصلاحية» per line. We keep
    the LATEST real date per barcode and drop the 3000 sentinel / blanks.
    """
    columns, data, _ = _parse_sheet(
        barcode_file, EXPIRY_MAP_HEADERS, {"barcode", "expiry"}, "تاريخ الصلاحية"
    )
    out = {}
    for _idx, cells in data:
        code = _norm_code(_cell(cells, columns, "barcode"))
        if not re.fullmatch(r"\d{6,}", code):
            continue
        d = _parse_expiry(_cell(cells, columns, "expiry"))
        if d is None:
            continue
        cur = out.get(code)
        if cur is None or d > cur:
            out[code] = d
    return out


def apply_expiry(store, barcode_expiry):
    """Fill Medication.expiry_date from {barcode: date} — ONLY where it's empty,
    so a re-import (or a manual edit) is never clobbered. Returns count set."""
    if not barcode_expiry:
        return 0
    codes = list(barcode_expiry.keys())
    to_update = []
    for i in range(0, len(codes), 1000):
        chunk = codes[i : i + 1000]
        for m in models.Product.objects.for_pharmacy(store).filter(
            expiry_date__isnull=True, barcode__in=chunk
        ).only("id", "barcode"):
            d = barcode_expiry.get(str(m.barcode).strip())
            if d is not None:
                m.expiry_date = d
                to_update.append(m)
    if to_update:
        models.Product.objects.bulk_update(
            to_update, ["expiry_date"], batch_size=1000
        )
    return len(to_update)


EXPIRY_REPORT_HEADERS = {
    "code": {"الرقم", "رقم الصنف", "رقم", "code", "الرقم الأصلي"},
    "name": {"اسم الصنف", "الصنف", "المادة", "الاسم", "name"},
    "expiry": {"تاريخ الصلاحية", "الصلاحية", "تاريخ الانتهاء", "expiry", "expiry date"},
    "qty": {"الكمية", "الرصيد", "الرصيد الحالي", "qty", "quantity", "العدد", "الكمية المتوفرة"},
}


def parse_expiry_report(expiry_file):
    """From Hesabate's «كشف تواريخ الصلاحية» (product-side expiry report).

    That report has no barcode — it carries item code (الرقم), name, quantity,
    batch and expiry. We return ({code→date}, {name→date}) keyed for matching by
    the product's own code (→ source_id) or name. Among a product's batches we
    keep the SOONEST real expiry that still has stock (qty > 0), since that's the
    one that will expire first. The 3000 sentinel and blanks are dropped.
    """
    columns, data, _ = _parse_sheet(
        expiry_file, EXPIRY_REPORT_HEADERS, {"name", "expiry"}, "تاريخ الصلاحية"
    )
    by_code, by_name = {}, {}
    has_qty = "qty" in columns
    for _idx, cells in data:
        d = _parse_expiry(_cell(cells, columns, "expiry"))
        if d is None:
            continue
        if has_qty:
            qty = _to_decimal(_cell(cells, columns, "qty"), default=Decimal("0"))
            if qty is not None and qty <= 0:
                continue  # only stock we still hold
        code = _norm_code(_cell(cells, columns, "code")) if "code" in columns else ""
        name = norm(_cell(cells, columns, "name")).lower()
        if code:
            cur = by_code.get(code)
            if cur is None or d < cur:  # soonest-expiring batch wins
                by_code[code] = d
        if name:
            cur = by_name.get(name)
            if cur is None or d < cur:
                by_name[name] = d
    return by_code, by_name


def apply_expiry_by_code_name(store, by_code, by_name):
    """Fill Medication.expiry_date from the {code}/{name} maps — ONLY where it's
    empty (never clobber a manual/prior value). Match on source_id first (exact,
    from the price-list code), then fall back to name. Returns count set."""
    if not by_code and not by_name:
        return 0
    to_update = []
    qs = (
        models.Product.objects.for_pharmacy(store)
        .filter(expiry_date__isnull=True)
        .only("id", "source_id", "name")
    )
    for m in qs.iterator(chunk_size=2000):
        sid = _norm_code(m.source_id) if m.source_id else ""
        d = by_code.get(sid) if sid else None
        if d is None:
            d = by_name.get(norm(m.name).lower())
        if d is not None:
            m.expiry_date = d
            to_update.append(m)
    if to_update:
        models.Product.objects.bulk_update(
            to_update, ["expiry_date"], batch_size=1000
        )
    return len(to_update)


def import_sales(store, invoices, items_by_invoice, name_barcode=None):
    """History-only import into ONE store: stock untouched, no debts made.

    Idempotent — invoices already imported (note 'Hesabate #<id>') are skipped,
    so re-uploading the same export is always safe.
    """
    existing = set()
    for note in models.Sale.objects.for_pharmacy(store).filter(
        note__startswith="Hesabate #"
    ).values_list("note", flat=True):
        existing.add(note.removeprefix("Hesabate #"))

    # Two lookups: by name (the legacy fallback) and by barcode (preferred, and
    # exact). Barcodes come from the optional item-movement report as a
    # {sale-name → barcode} map, resolved here against the catalogue's primary
    # AND alternate barcodes — the same keys the /price scanner matches on.
    name_barcode = name_barcode or {}
    meds = {}
    meds_by_barcode = {}
    for mid, mname, barcode, alt, category in models.Product.objects.for_pharmacy(
        store
    ).values_list("id", "name", "barcode", "alt_barcodes", "category__name"):
        meds[norm(mname).lower()] = (mid, category or "")
        if barcode:
            meds_by_barcode.setdefault(str(barcode).strip(), (mid, category or ""))
        for extra in (alt or []):
            if extra:
                meds_by_barcode.setdefault(str(extra).strip(), (mid, category or ""))

    tz = timezone.get_current_timezone()
    skipped = matched = matched_by_barcode = unmatched = 0

    # 1) Build the new Sale rows (skip already-imported invoices). We bulk-insert
    #    rather than create() per row — importing years of history was tens of
    #    thousands of round-trips (2 writes/invoice + 1/item) against Neon.
    sales, want_dt, fresh = [], [], []
    for inv in invoices:
        if inv["id"] in existing:
            skipped += 1
            continue
        dt = inv["dt"]
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, tz)
        sales.append(
            models.Sale(
                store=store,
                customer=None,
                payment_method=inv["payment"],
                total=(inv["amount"] + inv["discount"]).quantize(TWO),
                discounted_total=inv["amount"],
                note=f"Hesabate #{inv['id']}",
            )
        )
        want_dt.append(dt)
        fresh.append(inv)

    if not sales:
        return {
            "created_sales": 0, "created_items": 0, "skipped_existing": skipped,
            "matched_items": 0, "matched_by_barcode": 0, "unmatched_items": 0,
        }

    # 2) Insert, then back-date created_at in bulk (it's auto_now_add, so it
    #    can't be set on insert — one bulk UPDATE beats a query per invoice).
    models.Sale.objects.bulk_create(sales, batch_size=1000)
    for s, dt in zip(sales, want_dt):
        s.created_at = dt
    models.Sale.objects.bulk_update(sales, ["created_at"], batch_size=500)

    # 3) Build every line item and bulk-insert. line_total is computed in save(),
    #    which bulk_create skips, so set it here.
    items = []
    for sale, inv in zip(sales, fresh):
        for it in items_by_invoice.get(inv["id"], []):
            key = it["name"].lower()
            # Prefer barcode (exact); fall back to the legacy name match.
            code = name_barcode.get(key)
            hit = meds_by_barcode.get(code) if code else None
            if hit is not None:
                matched += 1
                matched_by_barcode += 1
            else:
                hit = meds.get(key)
                if hit is not None:
                    matched += 1
                else:
                    unmatched += 1
            price, qty = it["price"], it["qty"]
            items.append(
                models.SaleItem(
                    sale=sale,
                    product_id=hit[0] if hit else None,
                    medication_name=it["name"][:255],
                    category=(hit[1] if hit else "")[:120],
                    unit_price=price,
                    quantity=qty,
                    line_total=(Decimal(price or 0) * (qty or 0)).quantize(TWO),
                )
            )
    if items:
        models.SaleItem.objects.bulk_create(items, batch_size=1000)

    return {
        "created_sales": len(sales),
        "created_items": len(items),
        "skipped_existing": skipped,
        "matched_items": matched,
        "matched_by_barcode": matched_by_barcode,
        "unmatched_items": unmatched,
    }


def sales_match_stats(store, items_by_invoice, name_barcode=None):
    """Read-only preview of how sale lines WOULD match — nothing is written.

    Lets the dry-run show barcode coverage BEFORE committing, so a store can
    tell "82% of my lines link to a product by barcode" and decide whether to
    export a fuller item-movement report first. Mirrors import_sales' matching.
    """
    name_barcode = name_barcode or {}
    names, barcodes = set(), set()
    for mname, barcode, alt in models.Product.objects.for_pharmacy(
        store
    ).values_list("name", "barcode", "alt_barcodes"):
        names.add(norm(mname).lower())
        if barcode:
            barcodes.add(str(barcode).strip())
        for extra in (alt or []):
            if extra:
                barcodes.add(str(extra).strip())
    total = by_barcode = by_name = 0
    for lines in items_by_invoice.values():
        for it in lines:
            total += 1
            code = name_barcode.get(it["name"].lower())
            if code and code in barcodes:
                by_barcode += 1
            elif it["name"].lower() in names:
                by_name += 1
    return {
        "item_rows": total,
        "matched_by_barcode": by_barcode,
        "matched_by_name": by_name,
        "unmatched": total - by_barcode - by_name,
    }


def run_atomic(fn, *args, **kwargs):
    with transaction.atomic():
        return fn(*args, **kwargs)

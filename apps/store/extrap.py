"""Move a shop over from Extra P (the SalePro-based till it used before).

The shop's data is exported from Extra P once (products, categories, product
pictures, every sale with its lines and payments) into one JSON file, and this
module loads that file into ONE store:

    POST /api/v1/import/extrap/     multipart `file` (+ `dry_run`, `wipe`)

What a run does, in one transaction:

1.  With `wipe`: clears the menu (drinks, sizes, pictures, recipes, categories)
    and everything that is money or points — sales, returns, orders, debts,
    purchases, expenses, the cash drawer, the points ledger and balances.
    Customers, staff, inventory items (with their stock) and settings stay.
2.  Builds the menu from Extra P: its categories and products, prices, codes
    (kept as the barcode) and pictures.
3.  Loads every sale as HISTORY: the real date and time (from Extra P's payment
    record), cash or card, every line with its price. Nothing else moves —
    no stock is taken, no points are given, the cash drawer is untouched.

Re-running is safe: sales already loaded (note "extrap:<id>") are skipped, and
products are matched by their Extra P id, so a second, later export only adds
what is new. That is how the final cut-over works: load now, keep selling on
Extra P, and load the last few days again on the day of the switch.
"""
from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from apps.store import models

log = logging.getLogger(__name__)

TWO = Decimal("0.01")
NOTE = "extrap:"

#: Wiped by `wipe`, children before parents. (label, model, store filter)
WIPES: list[tuple[str, str, str]] = [
    ("المرتجعات", "SaleReturn", "store_id"),
    ("بنود الفواتير", "SaleItem", "sale__store_id"),
    ("مراجعات الفواتير", "SaleRevision", "sale__store_id"),
    ("الفواتير", "Sale", "store_id"),
    ("بنود الطلبات", "OrderItem", "order__store_id"),
    ("الطلبات", "Order", "store_id"),
    ("بنود الديون", "DebtItem", "debt__store_id"),
    ("الديون", "Debt", "store_id"),
    ("بنود المشتريات", "PurchaseItem", "order__store_id"),
    ("أوامر الشراء", "PurchaseOrder", "store_id"),
    ("سجل النقاط", "BeanLedger", "store_id"),
    ("أرصدة النقاط", "LoyaltyProfile", "store_id"),
    ("الإشعارات", "Notification", "store_id"),
    ("المصاريف المسجلة", "Expense", "store_id"),
    ("حركات الصندوق", "CashMove", "store_id"),
    ("جلسات الصندوق", "CashSession", "store_id"),
    ("سجل التغييرات", "AuditLog", "store_id"),
]

#: Appended to the old menu's drinks when they are hidden, so the menu page
#: tells them apart from Extra P's drinks of the same name.
OLD = " (قديم)"


def _fold(s: str) -> str:
    """Name key that ignores the spellings Arabic keyboards disagree on."""
    import re

    s = (s or "").strip()
    s = re.sub(r"[\u064B-\u0652\u0640]", "", s)  # harakat, tatweel
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s)


def hide_old_menu(store) -> dict:
    """The menu that was here before Extra P: hidden from the till, never
    deleted, so its recipes and sizes are still there to copy over.

    Its categories are reused when Extra P has one of the same name (ة/ه,
    أ/ا… ignored); any other old category is removed so the till does not show
    empty circles — the hidden drinks remember it in their metadata."""
    old = list(
        models.Product.objects.for_pharmacy(store.pk)
        .exclude(source_id__startswith=NOTE)
        .select_related("category")
    )
    for p in old:
        meta = dict(p.additional_metadata or {})
        if p.category_id:
            meta.setdefault("old_category", p.category.name)
        p.additional_metadata = meta
        p.is_active = False
        if not p.name.endswith(OLD):
            p.name = (p.name[: 255 - len(OLD)] + OLD)
    models.Product.objects.bulk_update(old, ["additional_metadata", "is_active", "name"], batch_size=500)
    return {"hidden": len(old)}


def _drop_unused_old_categories(store, keep_ids) -> int:
    gone = models.Category.objects.for_pharmacy(store.pk).exclude(pk__in=keep_ids)
    models.Product.objects.for_pharmacy(store.pk).filter(category__in=gone, is_active=False).update(category=None)
    n, _ = gone.delete()
    return n


def _model(name):
    return getattr(models, name)


def _has_field(model, name) -> bool:
    try:
        model._meta.get_field(name.split("__")[0])
        return True
    except Exception:
        return False


def wipe(store) -> list[tuple[str, int]]:
    done = []
    for label, name, field in WIPES:
        model = getattr(models, name, None)
        if model is None or not _has_field(model, field):
            continue
        n, _ = model.unguarded.filter(**{field: store.pk}).delete()
        done.append((label, n))
    return done


def _dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else default).replace(",", "")).quantize(TWO)
    except (InvalidOperation, ValueError):
        return Decimal(default).quantize(TWO)


def _when(s: str):
    """Extra P local time 'YYYY-MM-DDTHH:MM:SS' → aware datetime in the shop's zone."""
    dt = datetime.fromisoformat(s)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _picture(p: dict, request=None) -> str:
    raw = p.get("image_b64") or ""
    data = b""
    if raw:
        try:
            data = base64.b64decode(raw)
        except (binascii.Error, ValueError):
            data = b""
    elif p.get("image_url"):
        # Extra P serves product pictures publicly; fetched server-side so the
        # export file stays small. A picture that will not download is skipped,
        # never fatal — the owner can add it on the menu page.
        try:
            import requests

            r = requests.get(p["image_url"], timeout=15)
            if r.ok and r.headers.get("content-type", "").startswith("image/"):
                data = r.content
        except Exception:  # noqa: BLE001
            data = b""
    if not data:
        return ""
    from apps.core.uploads import store_upload

    ext = (p.get("image_ext") or "jpg").lstrip(".").lower()[:5]
    return store_upload(ContentFile(data, name=f"extrap.{ext}"), "products", request=request)


def load(store, dump: dict, *, request=None, do_wipe: bool = False, dry_run: bool = False, user=None) -> dict:
    """Load an Extra P export into `store`. One transaction; returns a report."""
    if (dump or {}).get("source") != "extrap":
        raise ValueError("هذا ليس ملف تصدير من Extra P.")

    report: dict = {"wiped": [], "categories": 0, "products": 0, "pictures": 0}
    with transaction.atomic():
        if do_wipe:
            report["wiped"] = wipe(store)
            report["old_menu"] = hide_old_menu(store)

        # ── categories (an old one of the same name is reused) ───────────
        existing = {_fold(c.name): c for c in models.Category.objects.for_pharmacy(store.pk)}
        cats = {}
        for name in dump.get("categories", []):
            name = (name or "").strip()[:120]
            if not name:
                continue
            c = existing.get(_fold(name))
            if c is None:
                c = models.Category.objects.create(store=store, name=name)
                existing[_fold(name)] = c
                report["categories"] += 1
            cats[name] = c
        if do_wipe:
            report["old_categories_removed"] = _drop_unused_old_categories(store, [c.pk for c in cats.values()])

        # ── products (matched by Extra P id) ─────────────────────────────
        by_src = {
            p.source_id: p
            for p in models.Product.objects.for_pharmacy(store.pk).filter(source_id__startswith=NOTE)
        }
        for p in dump.get("products", []):
            src = f"{NOTE}{p['id']}"
            if src in by_src:
                continue
            cat = cats.get((p.get("category") or "").strip())
            obj = models.Product.objects.create(
                store=store,
                source_id=src,
                name=(p.get("name") or "").strip()[:255] or f"صنف {p['id']}",
                barcode=(p.get("code") or "").strip()[:120],
                price=_dec(p.get("price")),
                category=cat,
                is_active=bool(p.get("active", True)),
                stock=0,
            )
            url = "" if dry_run else _picture(p, request)
            if url:
                obj.image = url
                obj.save(update_fields=["image", "updated_at"])
                report["pictures"] += 1
            by_src[src] = obj
            report["products"] += 1

        by_code = {}
        by_name = {}
        for obj in by_src.values():
            if obj.barcode:
                by_code.setdefault(obj.barcode, obj)
            by_name.setdefault(obj.name, obj)
        cat_of = {obj.pk: (obj.category.name if obj.category_id else "") for obj in by_src.values()}

        # ── sales (history only) ─────────────────────────────────────────
        # Earlier runs stamped the importer as the seller; undo that.
        models.Sale.objects.for_pharmacy(store.pk).filter(
            note__startswith=NOTE, created_by__isnull=False
        ).update(created_by=None)
        have = {
            n.split()[0]
            for n in models.Sale.objects.for_pharmacy(store.pk)
            .filter(note__startswith=NOTE)
            .values_list("note", flat=True)
        }
        sales, whens, fresh = [], [], []
        skipped = 0
        for s in dump.get("sales", []):
            note = f"{NOTE}{s['id']} {s.get('ref', '')}".strip()
            if f"{NOTE}{s['id']}" in have:
                skipped += 1
                continue
            when = _when(s["at"])
            sales.append(
                models.Sale(
                    store=store,
                    # Rung on Extra P, not by whoever ran the import: leaving
                    # it empty keeps 7,000 old sales off one employee's board.
                    created_by=None,
                    payment_method="card" if s.get("payment") == "card" else "cash",
                    is_return=bool(s.get("is_return")),
                    total=_dec(s.get("total")),
                    discounted_total=_dec(s.get("grand_total")),
                    receipt_code=(
                        f"{when:%y%m%d}{int(s['id']) % 1_000_000:06d}"
                        if str(s["id"]).isdigit() else ""
                    ),
                    note=note,
                )
            )
            whens.append(when)
            fresh.append(s)

        models.Sale.objects.bulk_create(sales, batch_size=1000)
        for sale, when in zip(sales, whens):
            sale.created_at = when
        models.Sale.objects.bulk_update(sales, ["created_at"], batch_size=500)

        lines, matched, unmatched = [], 0, 0
        for sale, s in zip(sales, fresh):
            for it in s.get("items", []):
                hit = by_code.get((it.get("code") or "").strip()) or by_name.get((it.get("name") or "").strip())
                if hit:
                    matched += 1
                else:
                    unmatched += 1
                qty = _dec(it.get("qty"), "1")
                price = _dec(it.get("unit_price"))
                lines.append(
                    models.SaleItem(
                        sale=sale,
                        product=hit,
                        medication_name=(it.get("name") or "")[:255],
                        category=(cat_of.get(hit.pk, "") if hit else "")[:120],
                        unit_price=price,
                        quantity=qty,
                        line_total=(price * qty).quantize(TWO),
                    )
                )
        models.SaleItem.objects.bulk_create(lines, batch_size=2000)
        when_of = {sale.pk: w for sale, w in zip(sales, whens)}
        for ln in lines:
            ln.created_at = when_of[ln.sale_id]
        models.SaleItem.objects.bulk_update(lines, ["created_at"], batch_size=1000)

        report.update(
            sales=len(sales),
            sales_skipped=skipped,
            lines=len(lines),
            lines_matched=matched,
            lines_unmatched=unmatched,
            cash=str(sum((x.discounted_total for x in sales if x.payment_method == "cash" and not x.is_return), Decimal("0"))),
            card=str(sum((x.discounted_total for x in sales if x.payment_method == "card" and not x.is_return), Decimal("0"))),
            returns=str(sum((x.discounted_total for x in sales if x.is_return), Decimal("0"))),
            first=min(whens).isoformat() if whens else None,
            last=max(whens).isoformat() if whens else None,
        )

        if dry_run:
            transaction.set_rollback(True)
            report["dry_run"] = True

    from apps.store.views import invalidate_pos_catalog_cache

    try:
        invalidate_pos_catalog_cache(store.pk)
    except Exception:  # pragma: no cover - cache is best-effort
        pass
    log.warning("extrap import store=%s %s", store.slug, {k: v for k, v in report.items() if k != "wiped"})
    return report


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
import json  # noqa: E402

from rest_framework import permissions  # noqa: E402
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser  # noqa: E402
from rest_framework.response import Response  # noqa: E402
from rest_framework.views import APIView  # noqa: E402

from apps.core.permissions import StoreResolved  # noqa: E402


class ExtrapImportView(APIView):
    """POST /api/v1/import/extrap/ — owner only. `file` = the export JSON.
    `dry_run=1` counts and rolls back; `wipe=1` clears the menu, sales, money
    and points first (customers, staff and inventory stay)."""

    permission_classes = [permissions.IsAuthenticated, StoreResolved]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        from apps.store.cafe_api import _is_owner
        from apps.store.views import request_pharmacy_id

        if not _is_owner(request.user):
            return Response({"detail": "للمالك فقط."}, status=403)
        store = models.Store.objects.get(pk=request_pharmacy_id(request))
        f = request.FILES.get("file")
        if f is None:
            return Response({"detail": "أرفق ملف التصدير."}, status=400)
        try:
            dump = json.loads(f.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response({"detail": "الملف ليس JSON صالحاً."}, status=400)
        flag = lambda k: str(request.data.get(k, "")).lower() in ("1", "true", "yes")  # noqa: E731
        try:
            report = load(
                store, dump, request=request,
                do_wipe=flag("wipe"), dry_run=flag("dry_run"), user=request.user,
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        return Response(report)

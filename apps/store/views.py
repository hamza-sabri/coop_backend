import re
from urllib.parse import quote

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import (
    Case, Count, DecimalField, F, Max, Q, Sum, Value, When,
)
from django.db.models.functions import Coalesce, TruncDate, TruncMonth
from django.utils import timezone
from rest_framework import filters, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

import django_filters

from apps.core.permissions import (
    ModuleEnabled,
    OwnerRequired,
    StoreRequired,
    StoreResolved,
)

from apps.accounts.clerk import ClerkAuthentication, IsClerkCustomer
from apps.accounts.firebase import (
    FirebaseAuthentication,
    IsAppCustomer,
    identity_filter,
)
from apps.store import points as points_service
from apps.store import push as push_service
from . import models, scan_tracking, serializers


class SaleFilter(django_filters.FilterSet):
    """Rich filtering for the print picker / sales history."""

    created_after = django_filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    created_before = django_filters.DateFilter(field_name="created_at", lookup_expr="date__lte")
    min_price = django_filters.NumberFilter(field_name="discounted_total", lookup_expr="gte")
    max_price = django_filters.NumberFilter(field_name="discounted_total", lookup_expr="lte")
    created_by = django_filters.NumberFilter(field_name="created_by")
    # Plain id filter (NOT ModelChoiceFilter): tenant models have guarded
    # managers, and the viewset's store scoping already makes foreign ids
    # inert — they simply match nothing.
    customer = django_filters.NumberFilter(field_name="customer_id")
    # The one search box on the sales history and the print picker.
    #
    # Whatever the cashier puts in it has to work, because she cannot be
    # expected to know which kind of number she is holding:
    #
    #   * a product name, typed          → "لبن"
    #   * a PRODUCT barcode, scanned     → 7290001234567
    #   * an INVOICE barcode, scanned off a receipt → 260819251195
    #   * that invoice number typed from a smudged receipt, or read off an
    #     older one that printed the bare sale id → 145868
    #
    # The last two were missing, which is why scanning a receipt found nothing
    # even though the number was printed on it and stored on the sale. The
    # receipt-code half is an EXACT match on purpose: a 12-digit code must not
    # fuzzy-match its way into a product barcode's results.
    item = django_filters.CharFilter(method="filter_item")

    def filter_item(self, queryset, name, value):
        value = (value or "").strip()
        if not value:
            return queryset
        # A NUMBER is a receipt, not a product.
        #
        # The owner scans invoice barcodes here all day and never scans a
        # product barcode into this box. Matching both meant every scan also
        # ran two icontains joins across 145,647 sales' line items — seconds of
        # waiting, on an indexed exact-match lookup that should be instant.
        #
        # So a numeric term hits `receipt_code` (indexed) and the primary key
        # only. No joins, no icontains, no scan of the item table.
        if value.isdigit():
            q = Q(receipt_code=value)
            if len(value) <= 12:
                q |= Q(receipt_code=value.zfill(12))
            # A bare sale id, from a receipt printed before receipt codes
            # existed. Capped at 9 digits so a 13-digit EAN is never read as a
            # primary key.
            if len(value) <= 9:
                q |= Q(pk=int(value))
            return queryset.filter(q)

        # Text still searches what a person would mean by it.
        return queryset.filter(
            Q(items__medication_name__icontains=value)
            | Q(items__product__barcode__icontains=value)
        ).distinct()

    class Meta:
        model = models.Sale
        fields = [
            "customer",
            "payment_method",
            "is_return",
            "created_by",
            "created_after",
            "created_before",
            "min_price",
            "max_price",
            "item",
        ]


class DebtFilter(django_filters.FilterSet):
    """?customer=<id> ?is_paid= — id filter instead of ModelChoiceFilter so
    the guarded Customer manager is never queried unscoped; the viewset's
    store scoping makes cross-tenant ids match nothing."""

    customer = django_filters.NumberFilter(field_name="customer_id")

    class Meta:
        model = models.Debt
        fields = ["customer", "is_paid"]


class MedicationVariantFilter(django_filters.FilterSet):
    """?product=<id> ?barcode= ?is_active= — same id-filter rationale."""

    product = django_filters.NumberFilter(field_name="product_id")

    class Meta:
        model = models.ProductVariant
        fields = ["product", "barcode", "is_active"]


# Every endpoint here requires a logged-in (staff) account — this is an internal
# tool holding customer PII and debts. Reads and writes both need a JWT.

# --- Redis-backed caching ----------------------------------------------------
# Aggregate endpoints (dashboard KPIs, med-catalogue stats) hit every page load,
# so they're cached and explicitly invalidated on any relevant write. TTLs are a
# safety net in case an invalidation is missed.

# All keys are per-store — tenants never share a cache entry.
def dashboard_key(pid):
    return f"store:{pid}:dashboard_stats:v2"


def med_stats_key(pid):
    return f"store:{pid}:med_stats:v2"


def sales_stats_key(pid):
    return f"store:{pid}:sales_stats:v2"


def pos_catalog_key(pid):
    # v5: variants now carry pack_size, so an offline device can tell a box
    # from a flavour without asking the server.
    return f"store:{pid}:pos_catalog:v5"


def catalog_version_key(pid):
    return f"store:{pid}:catalog_version:v1"


def customers_quick_key(pid):
    # v4 adds signed_up, so the till can float app customers to the front.
    # Bumping the version rather than waiting out the TTL means the new field
    # is there on the next request, not in ten minutes — a cached older
    # payload would look exactly like a broken feature.
    return f"store:{pid}:customers_quick:v4"


DASHBOARD_TTL = 5 * 60
MED_STATS_TTL = 15 * 60
SALES_STATS_TTL = 5 * 60
POS_CATALOG_TTL = 10 * 60
CUSTOMERS_QUICK_TTL = 10 * 60


def invalidate_dashboard_cache(pid):
    cache.delete(dashboard_key(pid))


def invalidate_med_stats_cache(pid):
    cache.delete(med_stats_key(pid))
    # The owner reports summary (dead-stock, low-stock & the other inventory
    # issues, plus the sales block) is derived from the SAME catalogue + sales
    # data, so it goes stale on exactly the writes that touch med stats — a
    # sale, a purchase, or a product edit, all of which call this. Clear it
    # here so the reports page refreshes instantly instead of waiting out its
    # 5-minute TTL. Cheap (a few key deletes); the recompute happens only on the
    # next reports view, not on the write.
    invalidate_reports_cache(pid)


def invalidate_sales_stats_cache(pid):
    cache.delete(sales_stats_key(pid))


def invalidate_pos_catalog_cache(pid):
    cache.delete(pos_catalog_key(pid))
    # The version fingerprint must move the moment the catalogue does — this
    # is what lets online devices notice a price change within seconds.
    cache.delete(catalog_version_key(pid))


def invalidate_customers_quick_cache(pid):
    cache.delete(customers_quick_key(pid))


def invalidate_reports_cache(pid):
    # Owner reports (inventory summary + sales analytics) are cached per
    # (pid, days). An import changes the underlying data, so clear the preset
    # windows the UI offers (7 / 30 / 90 days) — otherwise the report shows
    # stale numbers until the 5-minute TTL lapses.
    for d in (7, 30, 90):
        cache.delete(f"reports:summary:v3:{pid}:{d}")
        cache.delete(f"reports:sales:v1:{pid}:{d}")


def get_catalog_version(pid):
    """Catalogue fingerprint (counts + latest updated_at) used to namespace
    version-keyed caches (e.g. the public price-check). It changes the moment any
    product/variant is added, edited, or removed, so those caches refresh
    within seconds of an edit. Recomputed at most every 15s; the version key is
    deleted by invalidate_pos_catalog_cache on writes for instant freshness."""
    v = cache.get(catalog_version_key(pid))
    if v is None:
        m = models.Product.objects.for_pharmacy(pid).aggregate(
            n=Count("id"), t=Max("updated_at")
        )
        mv = models.ProductVariant.objects.for_pharmacy(pid).aggregate(
            n=Count("id"), t=Max("updated_at")
        )
        v = (
            f"{m['n']}:{m['t'].isoformat() if m['t'] else '0'}:"
            f"{mv['n']}:{mv['t'].isoformat() if mv['t'] else '0'}"
        )
        cache.set(catalog_version_key(pid), v, 15)
    return v


def request_pharmacy_id(request):
    """Tenant identity comes ONLY from the authenticated user — never from
    query params, headers, or bodies. No store → no data, full stop.

    Also the single choke-point for **billing suspension**: a store with
    `is_active=False` (non-payment) gets no data access at all. Because every
    tenant-scoped read/write funnels through here, one guard locks the whole
    tenant out — even if their access token is still valid — without editing
    every viewset. The result is cached on the request to avoid re-querying.
    """
    pid = getattr(request.user, "store_id", None)
    if not pid:
        # Same 400 contract as the StoreResolved permission — this is the
        # backstop for any code path the permission does not front.
        raise StoreRequired()

    active = getattr(request, "_pharmacy_active", None)
    if active is None:
        store = getattr(request.user, "store", None)
        if store is not None:
            active = bool(store.is_active)
        else:
            active = (
                models.Store.objects.filter(pk=pid)
                .values_list("is_active", flat=True)
                .first()
            )
        request._pharmacy_active = active
    if not active:
        raise PermissionDenied(
            "اشتراك الصيدلية موقوف. يرجى التواصل مع الدعم لتفعيله."
        )
    return pid


class StoreScopedMixin:
    """Air-tight tenant scoping: every read is filtered by the requesting
    user's store BEFORE any other filter, and every write is stamped."""

    @property
    def store_id(self):
        return request_pharmacy_id(self.request)

    def get_permissions(self):
        # The tenant-API 400 guard rides along on EVERY scoped viewset: no
        # resolvable store → 400 "store_id is required" before any
        # queryset or serializer runs. Appended last so auth stays 401 for
        # anonymous callers and module/role gates keep their own 403s.
        perms = super().get_permissions()
        perms.append(StoreResolved())
        return perms

    def get_queryset(self):
        return super().get_queryset().filter(store_id=self.store_id)


class ProductFilter(django_filters.FilterSet):
    """`?category=` accepts an id OR a name.

    The serializer reads and writes `category` as a plain NAME string, so any
    client that round-trips a product naturally sends the name back — while the
    default FK filter only ever accepted a primary key and answered anything
    else with "not a valid choice". The category filter therefore returned an
    empty list on the inventory page, from a value the API had just handed out.
    """

    category = django_filters.CharFilter(method="by_category")

    class Meta:
        model = models.Product
        fields = ["brand", "barcode", "source_id", "category"]

    def by_category(self, queryset, name, value):
        """Match by NAME even when given an id.

        Filtering `category_id` directly looked obviously right and returned
        nothing: the id the POS circles hand back does not necessarily belong
        to the same Category row the products point at — categories are
        per-store rows and the catalogue has more than one row per name. The
        name is what the API exposes and what every client round-trips, so the
        name is what this matches on; the id is only a way of naming it.
        """
        value = (value or "").strip()
        if not value:
            return queryset
        if value.isdigit():
            label = (
                models.Category.objects.unscoped()
                .filter(pk=int(value))
                .values_list("name", flat=True)
                .first()
            )
            if label:
                return queryset.filter(category__name__iexact=label)
            return queryset.filter(category_id=int(value))
        return queryset.filter(category__name__iexact=value)


class MedicationViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    """CRUD + search/filter/sort for the med catalogue.

    - search:  ?search=  over name, barcode, brand, manufacturer, category
    - filter:  ?category= ?brand= ?manufacturer= ?barcode= ?source_id=
    - sort:    ?ordering=name | -price | stock ...  (prefix - for descending)
    """

    # unscoped() base is safe HERE ONLY because StoreScopedMixin filters
    # by the requesting user's store before anything else, every request.
    queryset = models.Product.objects.unscoped().select_related(
        "category", "manufacturer"
    ).prefetch_related("images")
    serializer_class = serializers.ProductSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleEnabled]
    # Inventory management needs the inventory module; the POS catalogue
    # action belongs to the POS module (a POS-only cashier can sell without
    # being able to edit the med list).
    MODULE_BY_ACTION = {"pos_catalog": "pos"}

    @property
    def required_module(self):
        return self.MODULE_BY_ACTION.get(getattr(self, "action", None), "inventory")

    # `category` so the POS filter circles page correctly — filtering the
    # current page client-side would silently hide everything on page 2.
    filterset_class = ProductFilter
    search_fields = [
        "name",
        "barcode",
        # The EXTRA barcodes, so typing any of a product's codes into the
        # inventory search finds it. Scanning already resolved them (the scan
        # path checks alt_barcodes) — typing did not, which meant the shop
        # could add a second code, scan it successfully, and then fail to find
        # the same product by searching for it.
        # icontains on a JSON column matches its text form; that is the same
        # mechanism the scan lookup and the reports search already use.
        "alt_barcodes",
        "brand",
        "manufacturer__name",
        "category__name",
        "source_id",
    ]
    ordering_fields = [
        "name", "price", "cost", "stock", "expiry_date", "created_at", "updated_at"
    ]
    ordering = ["name"]

    LOW_STOCK_MAX = 5

    def get_queryset(self):
        qs = super().get_queryset()
        # ?category= / ?manufacturer= still accept plain names (API-compatible
        # with the old char fields).
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category__name=category)
        manufacturer = self.request.query_params.get("manufacturer")
        if manufacturer:
            qs = qs.filter(manufacturer__name=manufacturer)
        # ?stock_state=in|low|out → available / running low (1..5) / missing.
        state = self.request.query_params.get("stock_state")
        if state == "in":
            qs = qs.filter(stock__gt=0)
        elif state == "low":
            qs = qs.filter(stock__gt=0, stock__lte=self.LOW_STOCK_MAX)
        elif state == "out":
            qs = qs.filter(stock__lte=0)
        # ?expiry=expired | soon → the actionable in-stock expiry buckets
        # (same logic as the insights counts, via the shared reports helper).
        expiry = self.request.query_params.get("expiry")
        if expiry:
            from . import reports

            qs = reports.filter_expiry(qs, expiry, self.store_id)
        # ?units=pack | variant | plain → how the product is sold.
        #
        # 404 of the shop's 2,398 products carry a box unit, and a box sells
        # for a different price than the piece inside it. Those are the rows
        # worth checking before a stocktake or a price change, and until now
        # there was no way to list them — the card badge said "1 أنواع" but
        # nothing could filter on it.
        #
        #   pack    → has at least one variant with a real pack size (عبوة)
        #   variant → has any variant at all, pack or not (colour, flavour…)
        #   plain   → sells as a single piece only, no variants
        units = self.request.query_params.get("units")
        if units == "pack":
            qs = qs.filter(variants__pack_size__gt=0).distinct()
        elif units == "variant":
            qs = qs.filter(variants__isnull=False).distinct()
        elif units == "plain":
            qs = qs.filter(variants__isnull=True)
        return qs

    def get_serializer_context(self):
        # Inject the store's near-expiry window once per request so the
        # serializer can compute each row's badge without an N+1 query.
        ctx = super().get_serializer_context()
        pid = getattr(self, "store_id", None)
        if pid:
            from . import reports

            ctx["expiry_alert_default"] = reports.expiry_default(pid)
        return ctx

    @action(detail=False, methods=["get"], url_path="export/hesabate")
    def export_hesabate(self, request):
        """All products in Hesabate's «قائمة الأسعار» column schema, so the
        store can re-import to update their data in Hesabate."""
        import io

        from django.http import HttpResponse
        from openpyxl import Workbook

        pid = self.store_id
        store = models.Store.objects.get(pk=pid)
        wb = Workbook()
        ws = wb.active
        ws.title = "قائمة الأسعار"[:31]
        ws.append([
            "الرقم", "الرقم الأصلي", "الاسم", "التكلفة", "باركود",
            "العلامة التجارية", "الشركة المنتجة", "الرصيد الحالي",
            "التصنيفات", "التصنيف", "الطراز", "اللون", "بونص",
            "ملاحظات", "باركود الوحدات", "مفرق",
        ])
        rows = (
            models.Product.objects.for_pharmacy(pid)
            .values(
                "source_id", "name", "cost", "barcode", "brand",
                "manufacturer__name", "stock", "category__name", "notes",
                "price",
            )
            .iterator()
        )
        for m in rows:
            ws.append([
                m["source_id"] or "",
                "",                                # الرقم الأصلي
                m["name"],
                float(m["cost"]),
                m["barcode"] or "",
                m["brand"] or "بلا",
                m["manufacturer__name"] or "بلا",
                float(m["stock"]),
                "",                                # التصنيفات
                m["category__name"] or "بلا",
                "", "", "",                        # الطراز / اللون / بونص
                m["notes"] or "",
                "",                                # باركود الوحدات (UOM later)
                f"{m['price']} شيكل",              # مفرق — Hesabate stores a string
            ])
        buf = io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(
            buf.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
        )
        resp["Content-Disposition"] = (
            f'attachment; filename="hesabate-products-{store.slug}.xlsx"'
        )
        return resp

    def perform_create(self, serializer):
        serializer.save(store_id=self.store_id)
        invalidate_med_stats_cache(self.store_id)
        invalidate_pos_catalog_cache(self.store_id)

    def perform_update(self, serializer):
        serializer.save()
        invalidate_med_stats_cache(self.store_id)
        invalidate_pos_catalog_cache(self.store_id)

    def perform_destroy(self, instance):
        instance.delete()
        invalidate_med_stats_cache(self.store_id)
        invalidate_pos_catalog_cache(self.store_id)

    def get_permissions(self):
        perms = super().get_permissions()
        # Bulk delete/edit rewrite catalogue data — owners only, never employees.
        if getattr(self, "action", None) in ("bulk_delete", "bulk_update"):
            perms.append(OwnerRequired())
        return perms

    @action(detail=False, methods=["post"])
    def bulk_delete(self, request):
        """Delete many of THIS store's products at once. Variants and
        gallery images cascade with each product. Sales and debts history
        is never lost: SaleItem/DebtItem reference meds with SET_NULL and
        keep their own name/price/total snapshots, so records survive intact.
        Body: {"ids": [...]} or {"all": true} to flush the whole list.
        Owner-only (see get_permissions)."""
        qs = models.Product.objects.for_pharmacy(self.store_id)
        if not request.data.get("all"):
            ids = request.data.get("ids")
            if not isinstance(ids, list) or not ids:
                return Response(
                    {"detail": "حدّد عناصر للحذف أو أرسل all=true."}, status=400
                )
            qs = qs.filter(id__in=ids)
        with transaction.atomic():
            deleted = qs.count()
            qs.delete()
        invalidate_med_stats_cache(self.store_id)
        # Also bumps the catalog version fingerprint so online POS devices
        # notice the flush within seconds.
        invalidate_pos_catalog_cache(self.store_id)
        return Response({"deleted": deleted})

    @action(detail=False, methods=["post"])
    def bulk_update(self, request):
        """Fix MANY products at once — the "clean up imported data" tool.

        Target either an explicit selection or EVERY product matching a report
        filter (so "3,000 items priced zero" can be fixed in one action):
            {"ids": [1,2,3], "changes": {...}}
            {"issue": "zero_price", "all_matching": true, "changes": {...}}
              (+ the same optional filter params the reports table accepts:
               search, category, manufacturer, price_min/max, stock_min/max,
               low_stock_threshold, dead_days, include_equal, name_min/max)

        `changes` may set: price, cost, stock, category, manufacturer, brand,
        expiry_date (null clears), expiry_alert_days, reorder_level — and the
        big one: `price_from_cost_margin` (e.g. 25 → price = cost × 1.25),
        which repairs zero-priced imports from the real cost in one pass.

        Atomic, tenant-scoped, owner-only. Returns {"updated": n}.
        """
        from decimal import Decimal, InvalidOperation

        from . import reports

        pid = self.store_id
        changes = request.data.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            return Response({"detail": "لا توجد تعديلات."}, status=400)

        # ---- Which rows? explicit ids, or everything matching the filter ----
        ids = request.data.get("ids")
        if isinstance(ids, list) and ids:
            qs = models.Product.objects.for_pharmacy(pid).filter(id__in=ids)
        elif request.data.get("all_matching"):
            p = request.data
            issue = (p.get("issue") or "all").strip()
            if issue != "all" and issue not in reports.ISSUES:
                return Response({"detail": "تقرير غير معروف."}, status=400)

            def _dec(v):
                if v in (None, ""):
                    return None
                try:
                    return Decimal(str(v))
                except InvalidOperation:
                    return None

            try:
                threshold = Decimal(str(p.get("low_stock_threshold") or "5"))
            except InvalidOperation:
                threshold = Decimal("5")
            qs = reports.build_filtered_queryset(
                pid,
                issue=issue,
                search=(p.get("search") or "").strip(),
                category=(p.get("category_filter") or "").strip(),
                manufacturer=(p.get("manufacturer_filter") or "").strip(),
                price_min=_dec(p.get("price_min")),
                price_max=_dec(p.get("price_max")),
                stock_min=_dec(p.get("stock_min")),
                stock_max=_dec(p.get("stock_max")),
                low_stock_threshold=threshold,
                dead_days=self._int_arg(p.get("dead_days"), 60, 7, 365),
                include_equal=str(p.get("include_equal") or "") in ("1", "true", "True"),
                name_min=self._int_arg(
                    p.get("name_min"), reports.NAME_MIN_DEFAULT, 1, 500
                ),
                name_max=self._int_arg(
                    p.get("name_max"), reports.NAME_MAX_DEFAULT, 1, 500
                ),
            )
        else:
            return Response(
                {"detail": "حدّد عناصر (ids) أو أرسل all_matching=true."}, status=400
            )

        # ---- Build the update ------------------------------------------------
        update: dict = {}

        def _money(key):
            try:
                return Decimal(str(changes[key]))
            except (InvalidOperation, TypeError, ValueError):
                return None

        for field in ("price", "cost", "stock", "reorder_level"):
            if field in changes and changes[field] not in (None, ""):
                v = _money(field)
                if v is None:
                    return Response({"detail": f"قيمة غير صالحة: {field}"}, status=400)
                if v < 0:
                    return Response(
                        {"detail": "لا يمكن استخدام قيمة سالبة."}, status=400
                    )
                update[field] = v

        if changes.get("brand") is not None:
            update["brand"] = str(changes["brand"]).strip()

        if "expiry_date" in changes:
            raw = changes["expiry_date"]
            if raw in (None, ""):
                update["expiry_date"] = None
            else:
                from datetime import datetime

                try:
                    update["expiry_date"] = datetime.strptime(
                        str(raw)[:10], "%Y-%m-%d"
                    ).date()
                except ValueError:
                    return Response({"detail": "تاريخ صلاحية غير صالح."}, status=400)

        if changes.get("expiry_alert_days") not in (None, ""):
            update["expiry_alert_days"] = self._int_arg(
                changes["expiry_alert_days"], 30, 1, 3650
            )

        # FK-by-name: created inside THIS store only.
        if changes.get("category"):
            cat, _ = models.Category.objects.get_or_create(
                store_id=pid, name=str(changes["category"]).strip()
            )
            update["category"] = cat
        if changes.get("manufacturer"):
            man, _ = models.Manufacturer.objects.get_or_create(
                store_id=pid, name=str(changes["manufacturer"]).strip()
            )
            update["manufacturer"] = man

        # price = cost × (1 + margin%) — repairs zero-priced imports en masse.
        margin = changes.get("price_from_cost_margin")
        price_from_cost = margin not in (None, "")
        if price_from_cost:
            try:
                m = Decimal(str(margin))
            except InvalidOperation:
                return Response({"detail": "نسبة ربح غير صالحة."}, status=400)
            if m < 0 or m > 1000:
                return Response({"detail": "نسبة الربح خارج المدى."}, status=400)
            factor = Decimal("1") + (m / Decimal("100"))

        if not update and not price_from_cost:
            return Response({"detail": "لا توجد تعديلات."}, status=400)

        # ---- Snapshot BEFORE values so the action can be undone -------------
        # Only the fields this call will actually change are captured, keeping
        # the log small and the undo exact.
        touched_fields = [f for f in update if f not in ("category", "manufacturer")]
        if "category" in update:
            touched_fields.append("category_id")
        if "manufacturer" in update:
            touched_fields.append("manufacturer_id")
        if price_from_cost:
            touched_fields.append("price")
        snapshot: dict = {}
        row_count = qs.count()
        if row_count <= models.AuditLog.UNDO_MAX_ROWS and touched_fields:
            for row in qs.values("id", *set(touched_fields)):
                rid = row.pop("id")
                snapshot[str(rid)] = {k: (str(v) if v is not None else None)
                                      for k, v in row.items()}

        with transaction.atomic():
            updated = 0
            if price_from_cost:
                # Only rows with a real cost can derive a price.
                priced = qs.filter(cost__gt=0).update(
                    price=F("cost") * factor,
                    **update,
                )
                # Rows without a cost keep their price but still receive the
                # other explicit changes — they ARE modified, so they count.
                others = qs.filter(cost__lte=0).update(**update) if update else 0
                updated = priced + others
                return_extra = {"priced_from_cost": priced}
            else:
                updated = qs.update(**update)
                return_extra = {}

            # Audit trail — written inside the SAME transaction as the change,
            # so a logged action always corresponds to a real one.
            label = reports.ISSUES.get(issue, "كل الأصناف") if not ids else "تحديد يدوي"
            audit = models.AuditLog.objects.create(
                store_id=pid,
                actor=request.user if request.user.is_authenticated else None,
                action=models.AuditLog.ACTION_BULK_UPDATE,
                summary=f"{label} — {updated} صنف",
                request={"changes": changes, "issue": issue if not ids else None,
                         "ids": ids if ids else None},
                before=snapshot,
                affected=updated,
            )

        invalidate_med_stats_cache(pid)
        invalidate_pos_catalog_cache(pid)
        invalidate_reports_cache(pid)
        return Response({
            "updated": updated,
            "audit_id": audit.id,
            "can_undo": audit.can_undo,
            **return_extra,
        })

    @staticmethod
    def _int_arg(value, default, lo, hi):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    @action(detail=False, methods=["post"])
    def seed_demo(self, request):
        """Populate THIS (empty) store with a copy of the demo catalogue —
        every product, price and variant from the store that has the most
        products. Only runs when the caller's catalogue is empty, so it can
        never overwrite real data."""
        from .cloning import clone_catalog

        pid = self.store_id
        if models.Product.objects.for_pharmacy(pid).exists():
            return Response(
                {"detail": "الكتالوج غير فارغ — احذف المنتجات أولاً ثم أعد المحاولة."},
                status=400,
            )
        source = (
            models.Store.objects.exclude(pk=pid)
            .annotate(n=Count("products"))
            .filter(n__gt=0)
            .order_by("-n")
            .first()
        )
        if source is None:
            return Response({"detail": "لا توجد بيانات تجريبية متاحة."}, status=404)
        target = models.Store.objects.get(pk=pid)
        stats = clone_catalog(source, target)
        invalidate_med_stats_cache(pid)
        invalidate_pos_catalog_cache(pid)
        return Response({"source": source.name, **stats})

    @action(detail=False, methods=["get"])
    def pos_catalog(self, request):
        """The whole catalogue in one compact, Redis-cached payload.

        `GET /api/v1/products/pos_catalog/` — the POS keeps this client-side
        so barcode scans resolve instantly with zero network round-trips.
        """
        pid = self.store_id
        cached = cache.get(pos_catalog_key(pid))
        if cached is not None:
            return Response(cached)
        variants_by_med = {}
        for v in models.ProductVariant.objects.for_pharmacy(pid).filter(
            is_active=True
        ).values(
            "id", "product_id", "label", "barcode", "price", "stock",
            "attributes", "pack_size",
        ):
            variants_by_med.setdefault(v["product_id"], []).append(
                {
                    "id": v["id"],
                    "label": v["label"],
                    "barcode": v["barcode"],
                    "price": v["price"],
                    "stock": v["stock"],
                    "attributes": v["attributes"],
                    # How many pieces are in the box. Carried offline so the
                    # inventory "له عبوة" filter answers from the cached
                    # catalogue instead of failing into a retry state.
                    "pack_size": v["pack_size"],
                }
            )
        rows = [
            {
                "id": r["id"],
                "name": r["name"],
                "barcode": r["barcode"],
                "alt_barcodes": r["alt_barcodes"] or [],
                "price": r["price"],
                "stock": r["stock"],
                "category": r["category__name"] or "",
                "variants": variants_by_med.get(r["id"], []),
            }
            for r in models.Product.objects.for_pharmacy(pid).values(
                "id", "name", "barcode", "alt_barcodes", "price", "stock",
                "category__name",
            )
        ]
        payload = {"count": len(rows), "results": rows}
        cache.set(pos_catalog_key(pid), payload, POS_CATALOG_TTL)
        return Response(payload)

    @action(detail=False, methods=["get"])
    def catalog_version(self, request):
        """A cheap fingerprint of THIS store's catalogue.

        `GET /api/v1/products/catalog_version/` — clients poll this tiny
        response and refetch the full pos_catalog only when it changes, so a
        price edited anywhere (admin, import, another device) reaches every
        online POS within seconds instead of the old 5-minute worst case.
        Counts catch deletes; Max(updated_at) catches edits (bulk paths set
        updated_at explicitly).
        """
        pid = self.store_id
        v = cache.get(catalog_version_key(pid))
        if v is None:
            m = models.Product.objects.for_pharmacy(pid).aggregate(
                n=Count("id"), t=Max("updated_at")
            )
            mv = models.ProductVariant.objects.for_pharmacy(pid).aggregate(
                n=Count("id"), t=Max("updated_at")
            )
            v = (
                f"{m['n']}:{m['t'].isoformat() if m['t'] else '0'}:"
                f"{mv['n']}:{mv['t'].isoformat() if mv['t'] else '0'}"
            )
            cache.set(catalog_version_key(pid), v, 15)
        return Response({"version": v})

    @action(detail=False, methods=["get"])
    def stats(self, request):
        """Catalogue KPIs computed in the DB: counts, stock, and inventory value.

        `GET /api/v1/products/stats/` — cached in Redis, invalidated on writes.
        """
        pid = self.store_id
        cached = cache.get(med_stats_key(pid))
        if cached is not None:
            return Response(cached)

        money = DecimalField(max_digits=18, decimal_places=2)
        units = DecimalField(max_digits=18, decimal_places=3)
        qs = models.Product.objects.for_pharmacy(pid)
        agg = qs.aggregate(
            total_items=Count("id"),
            in_stock=Count("id", filter=Q(stock__gt=0)),
            low_stock=Count(
                "id", filter=Q(stock__gt=0, stock__lte=self.LOW_STOCK_MAX)
            ),
            out_of_stock=Count("id", filter=Q(stock__lte=0)),
            total_units=Coalesce(
                Sum("stock", filter=Q(stock__gt=0)),
                Decimal("0"),
                output_field=units,
            ),
            retail_value=Coalesce(
                Sum(F("price") * F("stock"), filter=Q(stock__gt=0), output_field=money),
                Decimal("0.00"),
                output_field=money,
            ),
            cost_value=Coalesce(
                Sum(F("cost") * F("stock"), filter=Q(stock__gt=0), output_field=money),
                Decimal("0.00"),
                output_field=money,
            ),
        )
        vagg = models.ProductVariant.objects.for_pharmacy(pid).filter(
            is_active=True
        ).aggregate(
            v_units=Coalesce(
                Sum("stock", filter=Q(stock__gt=0)),
                Decimal("0"),
                output_field=units,
            ),
            v_retail=Coalesce(
                Sum(F("price") * F("stock"), filter=Q(stock__gt=0), output_field=money),
                Decimal("0.00"),
                output_field=money,
            ),
            v_cost=Coalesce(
                Sum(F("cost") * F("stock"), filter=Q(stock__gt=0), output_field=money),
                Decimal("0.00"),
                output_field=money,
            ),
        )
        agg["total_units"] = agg["total_units"] + vagg["v_units"]
        agg["retail_value"] = agg["retail_value"] + vagg["v_retail"]
        agg["cost_value"] = agg["cost_value"] + vagg["v_cost"]
        by_category = [
            {"category": r["category__name"], "count": r["count"]}
            for r in qs.filter(category__isnull=False)
            .values("category__name")
            .annotate(count=Count("id"))
            .order_by("-count")[:8]
        ]
        # How the catalogue is packaged. Shops that buy by the case have
        # carry a box unit; the owner needs to know how many rows he is about
        # to walk before he opens the "له عبوة" filter, and the number is a
        # cheap COUNT DISTINCT on an already-indexed FK.
        with_packs = (
            qs.filter(variants__pack_size__gt=0).distinct().count()
        )
        with_variants = qs.filter(variants__isnull=False).distinct().count()
        payload = {
            **agg,
            "by_category": by_category,
            "units": {
                "pack": with_packs,
                "variant": with_variants,
                "plain": max((agg.get("total_items") or 0) - with_variants, 0),
            },
        }
        cache.set(med_stats_key(pid), payload, MED_STATS_TTL)
        return Response(payload)


class MedicationVariantViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    """CRUD for a product's variants (sub-SKUs with their own price/stock)."""

    # unscoped() base: get_queryset() below re-filters by the requesting
    # user's store on every request.
    queryset = models.ProductVariant.objects.unscoped().select_related(
        "product"
    )
    serializer_class = serializers.ProductVariantSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleEnabled]
    required_module = "inventory"
    filterset_class = MedicationVariantFilter
    ordering_fields = ["label", "price", "stock", "created_at"]
    ordering = ["label"]

    def get_queryset(self):
        return (
            viewsets.ModelViewSet.get_queryset(self)
            .filter(product__store_id=self.store_id)
        )

    def _invalidate(self):
        invalidate_med_stats_cache(self.store_id)
        invalidate_pos_catalog_cache(self.store_id)

    def perform_create(self, serializer):
        serializer.save()
        self._invalidate()

    def perform_update(self, serializer):
        serializer.save()
        self._invalidate()

    def perform_destroy(self, instance):
        instance.delete()
        self._invalidate()


class PublicPriceCheckView(APIView):
    """Customer-facing price lookup — NO login, aggressively throttled.

    Shoppers scan a product in the aisle and see its price without queueing.
    Exposes only what a customer may see (name, price, category, in-stock
    yes/no) — never cost, stock counts, or anything else. Served from the
    Redis POS catalogue when warm; indexed exact-barcode DB hit otherwise.
    """

    authentication_classes = []
    # Anonymous, but still a TENANT endpoint: the store resolves from the
    # ?store= slug (baked into each store's QR/domain). No slug at all →
    # 400 "store_id is required"; an unknown slug still answers not-found,
    # never a cross-store lookup.
    permission_classes = [StoreResolved]
    pharmacy_slug_param = "store"
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "price_check"

    @staticmethod
    def _pharmacy_for(slug):
        """Resolve slug → (id, enabled_modules), cached. (0, []) = no such tenant."""
        if not slug or len(slug) > 64:
            return None
        key = f"store:slug:v2:{slug}"
        hit = cache.get(key)
        if hit is None:
            row = (
                models.Store.objects.filter(slug=slug, is_active=True)
                .values("id", "enabled_modules")
                .first()
            )
            hit = (row["id"], row["enabled_modules"] or []) if row else (0, [])
            cache.set(key, hit, 10 * 60)
        return hit if hit[0] else None

    @staticmethod
    def _public_variants(pid, price_by_med):
        """Active variants grouped by product id — public fields only.

        A variant with no price of its own shows at the PRODUCT's price: the
        variant price is the amount charged when that option is picked, and it
        defaults to the product price. `price_by_med` maps product id → the
        product's own price (Decimal). A variant is hidden only when neither it
        nor its product is priced. Double-scoped: med ids already come from a
        tenant query AND the variant query filters by store again.
        """
        from decimal import Decimal
        from apps.core.uploads import resolve_stored_url

        grouped = {}
        for v in models.ProductVariant.objects.for_pharmacy(pid).filter(
            product_id__in=list(price_by_med.keys()), is_active=True
        ).values("id", "product_id", "label", "price", "image", "attributes"):
            own = v["price"] or Decimal("0")
            eff = own if own > 0 else (price_by_med.get(v["product_id"]) or Decimal("0"))
            if eff <= 0:
                continue
            grouped.setdefault(v["product_id"], []).append(
                {
                    # The id was missing here, and its absence was not
                    # cosmetic: the app compared `picked?.id === v.id`, both
                    # undefined, so EVERY option matched and every chip drew
                    # itself as selected. It also meant an order could never
                    # record WHICH option was chosen.
                    "id": v["id"],
                    "label": v["label"],
                    "price": str(eff),
                    "image": resolve_stored_url(v["image"] or ""),
                    "attributes": v["attributes"] or {},
                }
            )
        return grouped

    def _search(self, pid, q):
        """Live suggestions by name or partial barcode, tenant-scoped.

        Returns only what a shopper may see (name, price, image, category,
        variants). A listing surfaces only when the store has priced it OR
        it has at least one priced variant — unpriced items stay invisible, so
        neither the shared catalog nor other tenants' prices can leak.
        """
        from apps.core.uploads import resolve_stored_url

        meds = list(
            models.Product.objects.for_pharmacy(pid)
            .filter(
                Q(name__icontains=q)
                | Q(barcode__istartswith=q)
                | Q(alt_barcodes__icontains=f'"{q}')
            )
            .values("id", "name", "price", "image", "video_url", "category__name")
            .order_by("name")[:12]
        )
        variants = self._public_variants(pid, {m["id"]: m["price"] for m in meds})
        results = []
        for m in meds:
            vs = variants.get(m["id"], [])
            priced = bool(m["price"]) and m["price"] > 0
            if not priced and not vs:
                continue
            results.append(
                {
                    "name": m["name"],
                    "price": str(m["price"]) if priced else None,
                    # THIS store's own photo only — never a shared-catalog
                    # default donated by another tenant.
                    "image": resolve_stored_url(m["image"] or ""),
                    "video_url": resolve_stored_url(m["video_url"]) if m["video_url"] else None,
                    "category": m["category__name"] or "",
                    "variants": vs,
                }
            )
        return Response({"results": results})

    def get(self, request):
        # Typed search → live suggestions; otherwise an exact-barcode lookup.
        # The empty answer keeps each mode's shape ({"results": []} vs
        # {"found": False}) so existing scanner clients are unaffected.
        q = (request.query_params.get("q") or "").strip()

        def _empty():
            return Response({"results": []} if q else {"found": False})

        # Which store? Baked into each client's QR/domain. A wrong or
        # missing slug returns nothing — there is no cross-store lookup.
        hit = self._pharmacy_for(
            (request.query_params.get("store") or "").strip()
        )
        if not hit:
            return _empty()
        pid, enabled = hit
        # Tenant hasn't subscribed to the public price-check module → the
        # endpoint simply doesn't exist for them (empty list = all modules).
        if enabled and "price_check" not in enabled:
            return _empty()

        if q:
            return self._search(pid, q) if len(q) <= 120 else Response({"results": []})

        barcode = (request.query_params.get("barcode") or "").strip()
        if not barcode or len(barcode) > 120:
            return Response({"found": False})

        # Serve the scan from Redis so a barcode scan doesn't hit the DB every
        # time (Neon cost). The key is namespaced by the catalogue version
        # (counts + max updated_at): a price/product edit changes the version so
        # the shopper sees fresh data within seconds; otherwise it's served from
        # cache for CACHE_TTL. Signed B2 image URLs are valid for 7 days — far
        # longer than this TTL — so caching the signed payload is safe. The key
        # includes pid, so tenants never share a cache entry.
        version = get_catalog_version(pid)
        ck = f"store:{pid}:pricecheck:v1:{version}:{barcode}"
        cached = cache.get(ck)
        if cached is not None:
            return Response(cached)
        payload = self._barcode_payload(pid, barcode)
        # Only cache real hits. A "not found" (unknown or not-yet-priced barcode)
        # is left uncached, so pricing an item makes it appear immediately rather
        # than after the version TTL — and such scans are rare + throttled anyway.
        if payload.get("found"):
            cache.set(ck, payload, self.CACHE_TTL)
        return Response(payload)

    #: How long a scanned-barcode result lives in Redis (well under the 7-day
    #: B2 signature validity). Freshness on edits comes from the version key.
    CACHE_TTL = 10 * 60

    def _barcode_payload(self, pid, barcode):
        """barcode → public payload dict. Every branch returns a plain dict so
        get() can cache the whole thing as one entry. (Logic unchanged; only the
        Response() wrapping moved to the caller.)"""
        from apps.core.uploads import resolve_stored_url

        # Exact scan: a variant's own barcode resolves to that variant. Its
        # price defaults to the product's price when it has none of its own.
        variant = (
            models.ProductVariant.objects.for_pharmacy(pid).filter(
                barcode=barcode,
                is_active=True,
            )
            .values(
                "label",
                "price",
                "image",
                "product__name",
                "product__price",
                "product__image",
                "product__video_url",
            )
            .first()
        )
        if variant is not None:
            from decimal import Decimal

            own = variant["price"] or Decimal("0")
            eff = own if own > 0 else (variant["product__price"] or Decimal("0"))
            if eff > 0:
                vpayload = {
                    "found": True,
                    "name": f"{variant['product__name']} — {variant['label']}",
                    "price": str(eff),
                    "image": resolve_stored_url(
                        variant["image"] or variant["product__image"] or ""
                    ),
                }
                if variant.get("product__video_url"):
                    vpayload["video_url"] = resolve_stored_url(
                        variant["product__video_url"]
                    )
                return vpayload

        med = (
            models.Product.objects.for_pharmacy(pid)
            .filter(barcode=barcode)
            .values("id", "name", "price", "image", "video_url")
            .first()
        )
        if med is None:
            # Unit/packaging barcode (alt_barcodes) — same product, same price.
            med = (
                models.Product.objects.for_pharmacy(pid)
                .filter(alt_barcodes__icontains=f'"{barcode}"')
                .values("id", "name", "price", "image")
                .first()
            )
        if med is None:
            return {"found": False}
        # THIS store hasn't priced the item AND it has no priced variant →
        # same answer as "we don't have it". The shared catalog and other
        # tenants' prices must never leak through the public endpoint.
        vs = self._public_variants(pid, {med["id"]: med["price"]}).get(med["id"], [])
        priced = bool(med["price"]) and med["price"] > 0
        if not priced and not vs:
            return {"found": False}
        # Customers see ONLY: name, price, image, variants — all of them THIS
        # store's own values. No shared-catalog fallback: another tenant's
        # photo must never appear on this store's price page.
        payload = {
            "found": True,
            "name": med["name"],
            "price": str(med["price"]) if priced else None,
            "image": resolve_stored_url(med["image"] or ""),
        }
        # Gallery photos (this store's own only) so the shopper can flip
        # through images, not just the main one.
        gallery = [
            resolve_stored_url(im.image)
            for im in models.ProductImage.objects.for_pharmacy(pid)
            .filter(product_id=med["id"])
            .order_by("position", "id")
        ]
        if gallery:
            payload["images"] = gallery
        # Optional product video, played inline on the price page.
        if med.get("video_url"):
            payload["video_url"] = resolve_stored_url(med["video_url"])
        if vs:
            payload["variants"] = vs
        return payload


class PublicProductQrView(APIView):
    """GET /public/product-qr/?store=<slug>&barcode=<code> → a PNG QR.

    A customer standing in the store scans a product, then shows this QR to
    a relative so they can open the same product on their own phone — no login,
    no app, no trip to the store.

    Public on purpose (the price page it points at is public), but deliberately
    narrow:

    * it can ONLY ever encode a URL on this store's own price page. The
      barcode is the sole caller-supplied part and it is percent-encoded into a
      query string, so this can never be turned into a QR pointing somewhere
      else — an open QR generator would otherwise be a nice phishing tool
      wearing a store's logo.
    * unknown or suspended tenant → 404, same as every other public endpoint.
    * throttled in the price-check bucket, and cached: the QR for a given
      (store, barcode) never changes.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "price_check"

    #: A barcode is digits/letters/dashes. Anything else is not a barcode and
    #: has no business being embedded in a URL we generate.
    BARCODE_RE = re.compile(r"^[A-Za-z0-9\-_.]{1,64}$")

    def get(self, request):
        import io

        from django.conf import settings as dj_settings
        from django.http import HttpResponse

        import qrcode
        from qrcode.constants import ERROR_CORRECT_M

        slug = (request.query_params.get("store") or "").strip()
        barcode = (request.query_params.get("barcode") or "").strip()
        if not self.BARCODE_RE.match(barcode):
            return Response({"detail": "باركود غير صالح."}, status=400)

        hit = PublicPriceCheckView._pharmacy_for(slug)
        if not hit:
            return Response({"detail": "غير موجود."}, status=404)
        pid, enabled = hit
        # Same gate the price-check itself uses: an empty list means ALL
        # modules (legacy tenants), so only an explicit list can exclude.
        if enabled and "price_check" not in enabled:
            return Response({"detail": "غير موجود."}, status=404)

        store = models.Store.objects.filter(id=pid).only("host", "slug").first()
        root = getattr(dj_settings, "PUBLIC_ROOT_DOMAIN", "clinixa.cloud")
        # A tenant on its own domain must get a QR for THAT domain, or the link
        # sends their customers to a host they don't recognise.
        host = store.host or f"{store.slug}.{root}"
        url = f"https://{host}/price?barcode={quote(barcode, safe='')}"

        qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=12, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#201f38", back_color="white").convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        resp = HttpResponse(buf.getvalue(), content_type="image/png")
        # Immutable for a day: the QR for a barcode is the same every time.
        resp["Cache-Control"] = "public, max-age=86400"
        return resp


class PublicScanLogView(APIView):
    """Anonymous, fire-and-forget scan counter for the price-check kiosk.

    The shopper's browser beacons here in the background after each scan (see
    the /price scanner) — no auth, no body it waits on, no response it reads. We
    just bump a per-day Redis counter for the store; the nightly
    `flush_scan_counters` command folds those into ScanDaily for the Reports
    "تقارير المسح" section. ALWAYS returns 204 (even on bad/missing input) so a
    beacon can never surface anything to the customer, and this never touches
    the price-lookup path.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "scan_log"

    def post(self, request):
        slug = (request.query_params.get("store") or "").strip()
        hit = PublicPriceCheckView._pharmacy_for(slug)
        if hit:
            pid, enabled = hit
            # Only for tenants that actually expose the price-check (same gate
            # as the lookup). Empty list = all modules.
            if not enabled or "price_check" in enabled:
                barcode = (request.query_params.get("barcode") or "").strip()
                if barcode and len(barcode) <= 120:
                    found = (request.query_params.get("found") or "").strip() in (
                        "1",
                        "true",
                        "True",
                        "yes",
                    )
                    scan_tracking.record_scan(pid, barcode, found=found)
        return Response(status=204)


class PublicStatsView(APIView):
    """Public, aggregate-only platform stats for the marketing site.

    Deliberately platform-wide and NON-identifying — this is the "network
    effect" number the strategy wants to advertise. Returns total shared-catalog
    products covered, total listings, active stores, and a coverage
    breakdown by category NAME. It exposes NO store names, prices, stock, or
    any per-tenant detail, so nothing here can identify or cross a tenant.
    Cached, and throttled like the other public endpoint.

    DELIBERATELY EXEMPT from the StoreResolved 400 guard: this is the one
    central marketing endpoint — platform-wide by design, no store exists.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "price_check"

    CACHE_KEY = "public:stats:v1"
    TTL = 10 * 60

    def get(self, request):
        cached = cache.get(self.CACHE_KEY)
        if cached is not None:
            return Response(cached)

        # unscoped() BY DESIGN: platform-wide, aggregate-only marketing
        # numbers — no names, prices, stock, or anything tenant-identifying.
        meds = models.Product.objects.unscoped()
        categories = [
            {"name": row["category__name"], "count": row["count"]}
            for row in (
                meds.filter(category__name__isnull=False)
                .exclude(category__name="")
                .values("category__name")
                .annotate(count=Count("id"))
                .order_by("-count")[:8]
            )
        ]
        payload = {
            # Distinct barcodes across all listings — the shared CatalogItem table
            # is being retired (tenant isolation) and must not be read anymore.
            "products": meds.exclude(barcode="").values("barcode").distinct().count(),
            "listings": meds.count(),
            "stores": models.Store.objects.filter(is_active=True).count(),
            "with_images": meds.exclude(image="").count(),
            "categories": categories,
        }
        cache.set(self.CACHE_KEY, payload, self.TTL)
        return Response(payload)


class PublicMenuView(APIView):
    """The menu, for the customer app.

    The shop PWA authenticates its customers through Clerk, not through this
    API's staff session — so it cannot call /products/ at all. Until this
    existed the customer app carried a hard-coded copy of the menu, which is
    the worst of both worlds: the till and the phone disagree the moment a
    price changes, and nobody finds out until someone is charged the wrong
    amount at the counter.

    Public, tenant-scoped by slug, and cached: a menu is read constantly and
    changes a few times a week.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "price_check"

    CACHE_TTL = 5 * 60

    def get(self, request):
        # The native app sends no slug on purpose. It has no hostname to read
        # one from, and a constant compiled into the binary is a constant that
        # can disagree with the store every other endpoint in this file
        # resolves through CLERK_STORE_SLUG — which is exactly how the phone
        # ended up showing an empty menu while the till was fine. One source
        # of truth: no slug means "this deployment's store".
        slug = (request.query_params.get("store") or "").strip() or getattr(
            settings, "CLERK_STORE_SLUG", ""
        )
        hit = PublicPriceCheckView._pharmacy_for(slug)
        if not hit:
            return Response({"detail": "غير موجود."}, status=404)
        store_id, _enabled = hit

        key = f"store:{store_id}:public_menu:v1"
        payload = cache.get(key)
        if payload is None:
            rows = (
                models.Product.objects.for_pharmacy(store_id)
                .select_related("category")
                # Availability now exists. A drink switched off in المنيو
                # disappears from the customer app within the cache window
                # instead of being orderable all day.
                .filter(is_active=True)
                .order_by("category__name", "name")
                .values(
                    "id", "name", "price", "image", "notes",
                    "category__name", "category__icon",
                )
            )
            price_by_med = {r["id"]: r["price"] for r in rows}
            variants = PublicPriceCheckView._public_variants(store_id, price_by_med)
            items = [
                {
                    "id": r["id"],
                    "name": r["name"],
                    # Decimal is not JSON — and the cache stores what we build
                    # here, so it has to be a string before it goes in.
                    "price": str(r["price"]),
                    "image": r["image"] or "",
                    "description": r["notes"] or "",
                    "category": r["category__name"] or "",
                    "category_icon": r["category__icon"] or "",
                    "variants": variants.get(r["id"], []),
                }
                for r in rows
            ]
            cats, seen = [], set()
            for it in items:
                c = it["category"]
                if c and c not in seen:
                    seen.add(c)
                    cats.append({"name": c, "icon": it["category_icon"]})
            payload = {"categories": cats, "items": items}
            cache.set(key, payload, self.CACHE_TTL)
        return Response(payload)


class PublicBrandingView(APIView):
    """Public per-tenant branding — store NAME + LOGO by slug, nothing else.

    Powers the white-labelled app chrome (sidebar, login, PWA manifest): each
    store's deployment asks for its own slug and renders that store's
    name/logo, falling back to the generic "فارما" brand when unset. Exposes
    no stock, prices, or anything sensitive — name and logo only, and only
    for ACTIVE tenants. Cached; throttled like the other public endpoints.
    """

    authentication_classes = []
    # Tenant endpoint: store resolves from ?store= — missing → 400
    # "store_id is required"; unknown/inactive slug keeps answering 404.
    permission_classes = [StoreResolved]
    pharmacy_slug_param = "store"
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "price_check"

    TTL = 10 * 60

    @staticmethod
    def _branding_for(slug):
        """slug → {"name", "logo"(raw stored value)} for an active tenant, cached."""
        if not slug or len(slug) > 64:
            return None
        key = f"store:branding:v1:{slug}"
        hit = cache.get(key)
        if hit is None:
            row = (
                models.Store.objects.filter(slug=slug, is_active=True)
                .values("name", "logo")
                .first()
            )
            hit = row or {}
            cache.set(key, hit, PublicBrandingView.TTL)
        return hit or None

    def get(self, request):
        from apps.core.uploads import resolve_stored_url

        branding = self._branding_for(
            (request.query_params.get("store") or "").strip()
        )
        if branding is None:
            return Response({"detail": "not found"}, status=404)
        # Signed fresh on every response (local crypto, no extra requests) so
        # a `b2://` marker never leaks and the URL is always valid.
        return Response(
            {
                "name": branding.get("name") or "",
                "logo": resolve_stored_url(branding.get("logo") or ""),
            }
        )


class PublicBrandingIconView(APIView):
    """The store logo as a ready-to-install PWA icon (square PNG).

    The manifest needs concrete 192/512 PNGs; tenants upload arbitrary logo
    images. This endpoint square-pads and resizes the stored logo with Pillow.
    `?maskable=1` adds the ~20% safe-zone padding maskable icons require, on
    the app's background colour. 404 when the tenant has no logo — the
    frontend then keeps the default icons. Result bytes are cached.
    """

    authentication_classes = []
    # Tenant endpoint: same slug rule as PublicBrandingView.
    permission_classes = [StoreResolved]
    pharmacy_slug_param = "store"
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "price_check"

    SIZES = (192, 512)
    TTL = 60 * 60
    #: Matches the frontend manifest's background_color.
    MASKABLE_BG = (245, 244, 251, 255)

    @staticmethod
    def _logo_bytes(stored: str) -> bytes | None:
        """Fetch the raw logo image for a stored value (b2://, /media, http)."""
        from django.core.files.storage import default_storage

        from apps.core.uploads import B2_SCHEME

        try:
            if stored.startswith(B2_SCHEME):
                with default_storage.open(stored[len(B2_SCHEME):]) as f:
                    return f.read()
            if stored.startswith("http://") or stored.startswith("https://"):
                import urllib.request

                with urllib.request.urlopen(stored, timeout=10) as resp:  # noqa: S310
                    return resp.read(8 * 1024 * 1024)
            # Local /media path (dev without B2).
            rel = stored.split("/media/", 1)[-1] if "/media/" in stored else stored
            with default_storage.open(rel.lstrip("/")) as f:
                return f.read()
        except Exception:
            return None

    @classmethod
    def _render_icon(cls, raw: bytes, size: int, maskable: bool) -> bytes | None:
        import io

        try:
            from PIL import Image

            img = Image.open(io.BytesIO(raw)).convert("RGBA")
        except Exception:
            return None
        # Content area: full canvas for "any", ~80% centered for maskable
        # (Chrome's safe zone is a 4/5 circle; keep the mark inside it).
        content = int(size * 0.8) if maskable else size
        img.thumbnail((content, content), Image.LANCZOS)
        bg = cls.MASKABLE_BG if maskable else (0, 0, 0, 0)
        canvas = Image.new("RGBA", (size, size), bg)
        canvas.paste(
            img,
            ((size - img.width) // 2, (size - img.height) // 2),
            img,
        )
        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue()

    def get(self, request):
        from django.http import HttpResponse

        branding = PublicBrandingView._branding_for(
            (request.query_params.get("store") or "").strip()
        )
        stored = (branding or {}).get("logo") or ""
        if not stored:
            return Response({"detail": "not found"}, status=404)

        try:
            size = int(request.query_params.get("size") or 192)
        except ValueError:
            size = 192
        if size not in self.SIZES:
            size = 192
        maskable = (request.query_params.get("maskable") or "") in ("1", "true")

        import hashlib

        key = (
            "store:branding:icon:v1:"
            f"{hashlib.sha1(stored.encode()).hexdigest()}:{size}:{int(maskable)}"
        )
        png = cache.get(key)
        if png is None:
            raw = self._logo_bytes(stored)
            png = self._render_icon(raw, size, maskable) if raw else None
            if png is None:
                return Response({"detail": "logo unreadable"}, status=404)
            cache.set(key, png, self.TTL)
        resp = HttpResponse(png, content_type="image/png")
        resp["Cache-Control"] = "public, max-age=3600"
        return resp


class ReportsBaseView(APIView):
    """Shared gate for the paid reports module.

    THREE locks, all server-side: authenticated + tenant module subscription
    ("reports" via plan/extras) + OWNER role. The sales page shows employees
    and un-subscribed tenants only a blurred teaser — these endpoints are
    where the real enforcement lives.
    """

    permission_classes = [
        permissions.IsAuthenticated,
        ModuleEnabled,
        OwnerRequired,
        StoreResolved,  # tenant-API 400 guard
    ]
    required_module = "reports"

    @property
    def store_id(self):
        return request_pharmacy_id(self.request)

    @staticmethod
    def _int(value, default, lo, hi):
        try:
            return max(lo, min(int(value), hi))
        except (TypeError, ValueError):
            return default


class ReportsSummaryView(ReportsBaseView):
    """GET /reports/summary/?days=30 — issues + valuation + categories + sales.

    CACHED per (store, days) for 5 minutes: the summary fans out into a
    dozen aggregates, which gets expensive on 20k+ listings. Reports are
    analytics, not live ops — five-minute staleness is the right trade.
    """

    TTL = 5 * 60

    def get(self, request):
        from . import reports

        days = self._int(request.query_params.get("days"), 30, 1, 365)
        # v3: payload gained out_of_stock/checks/meta — never serve the old shape.
        key = f"reports:summary:v3:{self.store_id}:{days}"
        data = cache.get(key)
        if data is None:
            data = reports.summary(self.store_id, days=days)
            cache.set(key, data, self.TTL)
        return Response(data)


class SalesReportsSummaryView(ReportsBaseView):
    """GET /reports/sales/summary/?days=30 — deep sales analytics.

    Its OWN paid module ("sales_reports"), separate from inventory reports:
    a tenant can buy either or both. Cached 5 min per (store, days).
    """

    required_module = "sales_reports"
    TTL = 5 * 60

    def get(self, request):
        from . import reports

        days = self._int(request.query_params.get("days"), 30, 1, 365)
        key = f"reports:sales:v1:{self.store_id}:{days}"
        data = cache.get(key)
        if data is None:
            data = reports.sales_summary(self.store_id, days=days)
            cache.set(key, data, self.TTL)
        return Response(data)


class ReportsCafeView(ReportsBaseView):
    """GET /reports/cafe/?days=30 — the coffee shop's own report.

    Not behind the "sales_reports" module. The inventory report and the deep
    sales report are add-ons for a shop that has stock and staff to analyse;
    this one is the ONLY report a café has, and putting the only report behind
    an upsell is how a product gets described as a demo.

    Cached five minutes per (store, days), like every other report: this is
    analytics, not the till.
    """

    TTL = 5 * 60

    def get(self, request):
        from . import reports

        days = self._int(request.query_params.get("days"), 30, 1, 365)
        key = f"reports:cafe:v1:{self.store_id}:{days}"
        data = cache.get(key)
        if data is None:
            data = reports.cafe_summary(self.store_id, days=days)
            cache.set(key, data, self.TTL)
        return Response(data)


class SalesReportsExportView(ReportsBaseView):
    """GET /reports/sales/export/?days=30 — the sales report as xlsx."""

    required_module = "sales_reports"

    def get(self, request):
        import io

        from django.http import HttpResponse

        from . import reports

        store = models.Store.objects.get(id=self.store_id)
        wb = reports.build_export_workbook(
            store,
            report="sales",
            days=self._int(request.query_params.get("days"), 30, 1, 365),
        )
        buf = io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(
            buf.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        resp["Content-Disposition"] = (
            f'attachment; filename="report-sales-{store.slug}.xlsx"'
        )
        return resp


class ReportsTeaserView(APIView):
    """GET /reports/teaser/ — REAL but limited numbers for the upsell card.

    Deliberately open to ANY authenticated staff member (no reports module,
    no owner role): a few counts + the top seller's name. Cached 10 min.
    """

    permission_classes = [permissions.IsAuthenticated, StoreResolved]

    TTL = 10 * 60

    def get(self, request):
        from . import reports

        pid = request_pharmacy_id(request)
        key = f"reports:teaser:v1:{pid}"
        data = cache.get(key)
        if data is None:
            data = reports.teaser(pid)
            cache.set(key, data, self.TTL)
        return Response(data)


class ReportsProductsView(ReportsBaseView):
    """GET /reports/products/?issue=zero_price&search=&page=&page_size=

    The products behind one inventory-issue KPI — or `issue=all` to run
    the advanced filters against the ENTIRE catalogue. Filterable, sortable,
    paginated. Knobs: low_stock_threshold, dead_days, include_equal, plus
    advanced ranges: price_min/price_max, stock_min/stock_max, category.
    """

    @staticmethod
    def _dec(value):
        from decimal import Decimal, InvalidOperation

        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    def get(self, request):
        from decimal import Decimal, InvalidOperation

        from . import reports

        issue = (request.query_params.get("issue") or "zero_price").strip()
        if issue != "all" and issue not in reports.ISSUES:
            return Response({"detail": "تقرير غير معروف."}, status=400)

        try:
            threshold = Decimal(request.query_params.get("low_stock_threshold") or "5")
        except InvalidOperation:
            threshold = Decimal("5")

        # One shared builder → the products table and the filtered charts always
        # agree (same issue, same advanced ranges, same name-length window).
        p = request.query_params
        qs = reports.build_filtered_queryset(
            self.store_id,
            issue=issue,
            search=(p.get("search") or "").strip(),
            category=(p.get("category") or "").strip(),
            manufacturer=(p.get("manufacturer") or "").strip(),
            price_min=self._dec(p.get("price_min")),
            price_max=self._dec(p.get("price_max")),
            stock_min=self._dec(p.get("stock_min")),
            stock_max=self._dec(p.get("stock_max")),
            low_stock_threshold=threshold,
            dead_days=self._int(p.get("dead_days"), 60, 7, 365),
            include_equal=(p.get("include_equal") or "") in ("1", "true"),
            name_min=self._int(p.get("name_min"), reports.NAME_MIN_DEFAULT, 1, 500),
            name_max=self._int(p.get("name_max"), reports.NAME_MAX_DEFAULT, 1, 500),
        )

        ordering = (request.query_params.get("ordering") or "name").lstrip()
        allowed = {"name", "price", "cost", "stock", "-name", "-price", "-cost", "-stock"}
        qs = qs.order_by(ordering if ordering in allowed else "name")

        page = self._int(request.query_params.get("page"), 1, 1, 10_000)
        page_size = self._int(request.query_params.get("page_size"), 25, 1, 200)
        total = qs.count()
        start = (page - 1) * page_size
        rows = [
            {
                "id": m["id"],
                "name": m["name"],
                "barcode": m["barcode"],
                "category": m["category__name"] or "",
                "price": str(m["price"]),
                "cost": str(m["cost"]),
                "stock": str(m["stock"]),
            }
            for m in qs.values(
                "id", "name", "barcode", "category__name", "price", "cost", "stock"
            )[start : start + page_size]
        ]
        return Response(
            {
                "issue": issue,
                "label": reports.ISSUES.get(issue, "كل الأصناف"),
                "count": total,
                "page": page,
                "page_size": page_size,
                "results": rows,
            }
        )


class AuditLogView(APIView):
    """GET  /api/v1/audit/            — recent destructive actions (owner-only)
    POST /api/v1/audit/<id>/undo/    — put a bulk edit back.

    Undo restores the exact previous value of every field the action changed,
    row by row, inside one transaction. It is refused once already undone, or
    when the action was too large to snapshot — we never pretend.
    """

    permission_classes = [
        permissions.IsAuthenticated,
        OwnerRequired,
        StoreResolved,
    ]

    @property
    def store_id(self):
        return request_pharmacy_id(self.request)

    def get(self, request):
        rows = (
            models.AuditLog.objects.for_pharmacy(self.store_id)
            .select_related("actor")[:50]
        )
        return Response({
            "results": [
                {
                    "id": r.id,
                    "action": r.action,
                    "action_label": r.get_action_display(),
                    "summary": r.summary,
                    "affected": r.affected,
                    "actor": (r.actor.get_username() if r.actor else ""),
                    "created_at": r.created_at.isoformat(),
                    "undone_at": r.undone_at.isoformat() if r.undone_at else None,
                    "can_undo": r.can_undo,
                }
                for r in rows
            ]
        })

    def post(self, request, pk=None):
        pid = self.store_id
        try:
            entry = models.AuditLog.objects.for_pharmacy(pid).get(pk=pk)
        except models.AuditLog.DoesNotExist:
            return Response({"detail": "غير موجود."}, status=404)
        if entry.undone_at:
            return Response({"detail": "تم التراجع عن هذا الإجراء مسبقاً."}, status=400)
        if not entry.can_undo:
            return Response(
                {"detail": "هذا الإجراء غير قابل للتراجع تلقائياً."}, status=400
            )

        from decimal import Decimal, InvalidOperation

        restored = 0
        with transaction.atomic():
            for rid, fields in entry.before.items():
                clean = {}
                for key, value in fields.items():
                    if value is None:
                        clean[key] = None
                    elif key in ("price", "cost", "stock", "reorder_level"):
                        try:
                            clean[key] = Decimal(value)
                        except InvalidOperation:
                            continue
                    elif key in ("category_id", "manufacturer_id", "expiry_alert_days"):
                        clean[key] = int(value)
                    elif key == "expiry_date":
                        clean[key] = value  # ISO date string is fine for the ORM
                    else:
                        clean[key] = value
                if clean:
                    restored += models.Product.objects.for_pharmacy(pid).filter(
                        pk=int(rid)
                    ).update(**clean)
            entry.undone_at = timezone.now()
            entry.undone_by = request.user if request.user.is_authenticated else None
            entry.save(update_fields=["undone_at", "undone_by", "updated_at"])

        invalidate_med_stats_cache(pid)
        invalidate_pos_catalog_cache(pid)
        invalidate_reports_cache(pid)
        return Response({"restored": restored})


class ReportsFilteredChartsView(ReportsBaseView):
    """GET /reports/filtered-charts/?issue=...&<same params as products>

    Valuation + category breakdown over the SAME filtered queryset the products
    table shows — so the two reports-page charts reflect the active filter.
    """

    @staticmethod
    def _dec(value):
        from decimal import Decimal, InvalidOperation

        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    def get(self, request):
        from decimal import Decimal, InvalidOperation

        from . import reports

        issue = (request.query_params.get("issue") or "all").strip()
        if issue != "all" and issue not in reports.ISSUES:
            return Response({"detail": "تقرير غير معروف."}, status=400)
        try:
            threshold = Decimal(request.query_params.get("low_stock_threshold") or "5")
        except InvalidOperation:
            threshold = Decimal("5")
        p = request.query_params
        qs = reports.build_filtered_queryset(
            self.store_id,
            issue=issue,
            search=(p.get("search") or "").strip(),
            category=(p.get("category") or "").strip(),
            manufacturer=(p.get("manufacturer") or "").strip(),
            price_min=self._dec(p.get("price_min")),
            price_max=self._dec(p.get("price_max")),
            stock_min=self._dec(p.get("stock_min")),
            stock_max=self._dec(p.get("stock_max")),
            low_stock_threshold=threshold,
            dead_days=self._int(p.get("dead_days"), 60, 7, 365),
            include_equal=(p.get("include_equal") or "") in ("1", "true"),
            name_min=self._int(p.get("name_min"), reports.NAME_MIN_DEFAULT, 1, 500),
            name_max=self._int(p.get("name_max"), reports.NAME_MAX_DEFAULT, 1, 500),
        )
        return Response(reports.filtered_charts(self.store_id, qs))


class ReportsTopProductsView(ReportsBaseView):
    """GET /reports/top-products/?days=30&by=qty|revenue&direction=top|bottom&limit=10"""

    def get(self, request):
        from . import reports

        return Response(
            {
                "results": reports.product_sales(
                    self.store_id,
                    days=self._int(request.query_params.get("days"), 30, 1, 365),
                    by=(request.query_params.get("by") or "qty"),
                    direction=(request.query_params.get("direction") or "top"),
                    limit=self._int(request.query_params.get("limit"), 10, 1, 100),
                    category=(request.query_params.get("category") or "").strip(),
                )
            }
        )


class DataExportView(APIView):
    """GET /export/ — EVERYTHING the store owns, as one xlsx with two sheets.

    Distinct from /reports/export/, which produces an *analysis* — filtered,
    ranked and capped. This one is a plain dump, for handing to an accountant
    or moving to another system. No limit, no aggregation, no options: one
    click, one file.

    Gating is OWNER only. It deliberately does NOT extend ReportsBaseView:
    that carries `required_module = "reports"`, and the owner of this store has
    only `inventory` + `pos` enabled, so every export 403'd with a message
    blaming his role. Exporting your own data is not a reports feature — it is
    the thing that stops the app being a lock-in, and it should work on any
    plan.

    Written with openpyxl's write_only workbook and a queryset iterator, so
    145k sales stream out a row at a time instead of building the whole sheet
    in memory first.
    """

    permission_classes = [
        permissions.IsAuthenticated,
        OwnerRequired,
        StoreResolved,  # tenant-API 400 guard
    ]

    @property
    def store_id(self):
        return request_pharmacy_id(self.request)

    def get(self, request):
        import io

        from django.http import HttpResponse
        from openpyxl import Workbook

        wb = Workbook(write_only=True)
        n_products = self._products(wb)
        n_sales = self._sales(wb)

        buf = io.BytesIO()
        wb.save(buf)
        stamp = timezone.now().strftime("%Y%m%d-%H%M")
        resp = HttpResponse(
            buf.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        # Named after the STORE this export came from, not after whichever
        # store the code was first written for. A file called "almawdah-…"
        # downloaded from a different shop is a support call at best and a
        # mixed-up spreadsheet at worst.
        store_slug = (
            models.Store.objects.filter(pk=self.store_id)
            .values_list("slug", flat=True)
            .first()
            or "store"
        )
        slug = re.sub(r"[^a-z0-9-]+", "-", store_slug.lower()).strip("-")
        resp["Content-Disposition"] = (
            f'attachment; filename="{slug or "store"}-{stamp}.xlsx"'
        )
        # Kept for the toast: total rows, plus the split per sheet.
        resp["X-Row-Count"] = str(n_products + n_sales)
        resp["X-Product-Count"] = str(n_products)
        resp["X-Sale-Count"] = str(n_sales)
        return resp

    def _products(self, wb) -> int:
        ws = wb.create_sheet("المنتجات")
        ws.append([
            "المعرّف", "الرمز", "الاسم", "الباركود", "التصنيف",
            "سعر البيع", "التكلفة", "المخزون", "تاريخ الانتهاء",
            "الماركة", "ملاحظات", "أُنشئ",
        ])
        rows = (
            models.Product.objects.for_pharmacy(self.store_id)
            .select_related("category")
            .order_by("id")
        )
        n = 0
        for p in rows.iterator(chunk_size=2000):
            ws.append([
                p.id, p.original_number or p.source_id, p.name, p.barcode,
                p.category.name if p.category_id else "",
                float(p.price or 0), float(p.cost or 0), float(p.stock or 0),
                p.expiry_date.isoformat() if p.expiry_date else "",
                p.brand, p.notes,
                p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "",
            ])
            n += 1
        return n

    def _sales(self, wb) -> int:
        ws = wb.create_sheet("المبيعات")
        ws.append([
            "رقم العملية", "التاريخ", "الزبون", "طريقة الدفع",
            "الإجمالي", "بعد الخصم", "مرتجع", "عدد الأصناف", "ملاحظات",
        ])
        rows = (
            models.Sale.objects.for_pharmacy(self.store_id)
            .select_related("customer")
            .annotate(n_items=Count("items"))
            .order_by("id")
        )
        n = 0
        for s in rows.iterator(chunk_size=2000):
            ws.append([
                s.id,
                s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else "",
                s.customer.name if s.customer_id else "",
                s.get_payment_method_display(),
                float(s.total or 0), float(s.discounted_total or 0),
                "نعم" if s.is_return else "لا",
                s.n_items, s.note,
            ])
            n += 1
        return n


class ReportsExportView(ReportsBaseView):
    """GET /reports/export/?report=issues|top_products|summary — xlsx download."""

    def get(self, request):
        import io
        from decimal import Decimal, InvalidOperation

        from django.http import HttpResponse
        from django.utils.text import slugify

        from . import reports

        report = (request.query_params.get("report") or "summary").strip()
        try:
            threshold = Decimal(request.query_params.get("low_stock_threshold") or "5")
        except InvalidOperation:
            threshold = Decimal("5")
        store = models.Store.objects.get(id=self.store_id)
        try:
            wb = reports.build_export_workbook(
                store,
                report=report,
                issue=(request.query_params.get("issue") or "zero_price").strip(),
                days=self._int(request.query_params.get("days"), 30, 1, 365),
                by=(request.query_params.get("by") or "qty"),
                limit=self._int(request.query_params.get("limit"), 20, 1, 500),
                low_stock_threshold=threshold,
                dead_days=self._int(request.query_params.get("dead_days"), 60, 7, 365),
                include_equal=(request.query_params.get("include_equal") or "")
                in ("1", "true"),
            )
        except ValueError:
            return Response({"detail": "تقرير غير معروف."}, status=400)

        buf = io.BytesIO()
        wb.save(buf)
        filename = f"report-{slugify(report)}-{store.slug}.xlsx"
        resp = HttpResponse(
            buf.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp


class PricePageQrView(APIView):
    """GET /qr/price-page/ — the store's price-check QR, logo in the middle.

    Renders a high-error-correction QR for https://<slug>.<root>/price with
    the tenant's logo centered on a white pad (30% of the QR stays readable
    even with the overlay — that's what ERROR_CORRECT_H is for). Owners only;
    meant for printing / sharing with customers. ?download=1 forces a file
    download.
    """

    permission_classes = [permissions.IsAuthenticated, OwnerRequired, StoreResolved]

    def get(self, request):
        import io

        from django.conf import settings as dj_settings
        from django.http import HttpResponse

        import qrcode
        from qrcode.constants import ERROR_CORRECT_H

        pid = request_pharmacy_id(request)
        store = models.Store.objects.get(id=pid)
        root = getattr(dj_settings, "PUBLIC_ROOT_DOMAIN", "clinixa.cloud")
        url = f"https://{store.slug}.{root}/price"

        qr = qrcode.QRCode(
            error_correction=ERROR_CORRECT_H, box_size=16, border=2
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#201f38", back_color="white").convert("RGB")

        logo_raw = (
            PublicBrandingIconView._logo_bytes(store.logo) if store.logo else None
        )
        if logo_raw:
            from PIL import Image, ImageDraw

            try:
                logo = Image.open(io.BytesIO(logo_raw)).convert("RGBA")
                side = img.size[0]
                # 30% slot — the most ERROR_CORRECT_H can safely cover — and
                # the logo is RESIZED to fill it (thumbnail only shrinks, so
                # small source logos used to render tiny and blurry).
                slot = int(side * 0.30)
                pad = int(slot * 0.08)
                box = slot - 2 * pad
                scale = min(box / logo.width, box / logo.height)
                logo = logo.resize(
                    (max(1, int(logo.width * scale)), max(1, int(logo.height * scale))),
                    Image.LANCZOS,
                )
                # White rounded pad behind the logo keeps the QR readable.
                patch = Image.new("RGBA", (slot, slot), (255, 255, 255, 0))
                draw = ImageDraw.Draw(patch)
                draw.rounded_rectangle(
                    [0, 0, slot - 1, slot - 1],
                    radius=int(slot * 0.18),
                    fill=(255, 255, 255, 255),
                )
                patch.paste(
                    logo,
                    ((slot - logo.width) // 2, (slot - logo.height) // 2),
                    logo,
                )
                pos = ((side - slot) // 2, (side - slot) // 2)
                img.paste(patch, pos, patch)
            except Exception:
                pass  # unreadable logo -> plain QR is still perfectly usable

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        resp = HttpResponse(buf.getvalue(), content_type="image/png")
        if request.query_params.get("download"):
            resp["Content-Disposition"] = (
                f'attachment; filename="qr-{store.slug}-price.png"'
            )
        return resp


class _TaxonomyViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    """Shared behaviour for the category / manufacturer dropdown endpoints.

    List is searchable (?search=) and ordered by usage; rows can be created
    directly, though writing a new name on a med auto-creates one anyway.
    """

    permission_classes = [permissions.IsAuthenticated, ModuleEnabled]
    required_module = "inventory"
    search_fields = ["name"]
    ordering_fields = ["name", "count", "created_at"]
    ordering = ["-count", "name"]

    def get_queryset(self):
        return super().get_queryset().annotate(count=Count("products"))

    def _invalidate(self):
        invalidate_med_stats_cache(self.store_id)
        invalidate_pos_catalog_cache(self.store_id)

    def perform_create(self, serializer):
        serializer.save(store_id=self.store_id)
        self._invalidate()

    def perform_update(self, serializer):
        serializer.save()
        self._invalidate()

    def perform_destroy(self, instance):
        instance.delete()
        self._invalidate()


class CategoryViewSet(_TaxonomyViewSet):
    # unscoped() base: StoreScopedMixin (via _TaxonomyViewSet) re-filters
    # by the requesting user's store on every request.
    queryset = models.Category.objects.unscoped()
    serializer_class = serializers.CategorySerializer


class ManufacturerViewSet(_TaxonomyViewSet):
    queryset = models.Manufacturer.objects.unscoped()  # scoped by the mixin
    serializer_class = serializers.ManufacturerSerializer


class CustomerViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    """CRUD + search/filter/sort for customer profiles.

    - search:  ?search=  over name, phone, status, notes
    - filter:  ?status= ?phone= ?gender=  ?has_debt=true|false
    - sort:    ?ordering=name | -created_at | -outstanding | outstanding ...
    Each row is annotated with `outstanding` = sum of unpaid debts.
    """

    serializer_class = serializers.CustomerSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleEnabled]
    # Customer profiles are shared plumbing: reachable by anyone who has the
    # customers module OR a module that needs them (debt ledger, POS credit
    # sales). Settling debts specifically requires the debts module.
    MODULE_BY_ACTION = {"settle": "debts"}

    @property
    def required_module(self):
        return self.MODULE_BY_ACTION.get(
            getattr(self, "action", None), ("customers", "debts", "pos")
        )

    filterset_fields = ["status", "phone", "gender"]
    search_fields = ["name", "phone", "status", "notes"]
    ordering_fields = [
        "name", "phone", "created_at", "updated_at", "outstanding", "points",
    ]
    # Best customers first. `points` is annotated (not the related column) so
    # that someone with no loyalty row sorts as zero instead of as NULL, which
    # on a DESC sort would have floated every stranger to the top of the list.
    ordering = ["-points", "name"]

    def get_queryset(self):
        qs = models.Customer.objects.for_pharmacy(self.store_id).annotate(
            outstanding=Coalesce(
                Sum("debts__discounted_total", filter=Q(debts__is_paid=False)),
                Decimal("0.00"),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
            points=Coalesce("loyalty__beans", Value(0)),
        )
        # ?has_debt=true → only customers who currently owe; false → clear ones.
        has_debt = self.request.query_params.get("has_debt")
        if has_debt in ("true", "1"):
            qs = qs.filter(outstanding__gt=0)
        elif has_debt in ("false", "0"):
            qs = qs.filter(outstanding__lte=0)
        return qs

    def perform_create(self, serializer):
        serializer.save(store_id=self.store_id)
        invalidate_dashboard_cache(self.store_id)
        invalidate_customers_quick_cache(self.store_id)

    def perform_update(self, serializer):
        serializer.save()
        invalidate_dashboard_cache(self.store_id)
        invalidate_customers_quick_cache(self.store_id)

    def perform_destroy(self, instance):
        instance.delete()
        invalidate_dashboard_cache(self.store_id)
        invalidate_customers_quick_cache(self.store_id)

    @action(detail=False, methods=["get"])
    def quick(self, request):
        """Every customer in one Redis-cached payload for instant client-side
        search/filtering (POS pickers). `GET /api/v1/customers/quick/`."""
        cached = cache.get(customers_quick_key(self.store_id))
        if cached is not None:
            return Response(cached)
        # avatar + beans ride along so the POS picker can show a face and a
        # balance without a request per customer.
        rows = [
            {
                "id": c["id"],
                "name": c["name"],
                "phone": c["phone"] or "",
                "outstanding": f'{c["outstanding"]:.2f}',
                "avatar": c["avatar"] or "",
                "beans": c["loyalty__beans"] or 0,
                # Someone who signed up in the app: the till sorts these first,
                # because they are the ones whose points actually move.
                "signed_up": bool(c["clerk_id"]),
            }
            for c in self.get_queryset().values(
                "id", "name", "phone", "outstanding", "avatar",
                "loyalty__beans", "clerk_id",
            )
        ]
        payload = {"count": len(rows), "results": rows}
        cache.set(customers_quick_key(self.store_id), payload, CUSTOMERS_QUICK_TTL)
        return Response(payload)

    @action(detail=True, methods=["get", "post"], url_path="points")
    def points(self, request, pk=None):
        """One customer's loyalty standing, and a way to move it by hand.

        `GET  /api/v1/customers/{id}/points/`
            {balance, earned, spent, redemptions, value_ils, points_per_ils,
             earn_rate, activity[]}

        `POST /api/v1/customers/{id}/points/`  {"delta": 25, "note": "..."}
            Signed. Positive adds, negative takes away — never a setter, so the
            ledger stays a list of things that happened rather than a column
            somebody overwrote. A negative delta is clipped at the balance: a
            customer cannot be pushed into a debt they have no way to read or
            clear.

        Idempotent per `client_uuid`. Without one, a cashier double-tapping
        "+20" on a slow connection credits forty.
        """
        import uuid as _uuid

        from apps.store import points as points_service

        customer = self.get_object()
        store = models.Store.objects.filter(pk=self.store_id).first()
        if store is None:
            return Response({"detail": "المقهى غير متاح حالياً"}, status=503)

        if request.method == "POST":
            try:
                delta = int(request.data.get("delta") or 0)
            except (TypeError, ValueError):
                return Response({"detail": "delta غير صالح"}, status=400)
            if delta == 0:
                return Response({"detail": "لا يوجد تغيير"}, status=400)
            # A cap, because this is a manual field on a till: a slipped digit
            # should be a rejected request, not a customer with 50,000 points.
            if abs(delta) > 100000:
                return Response({"detail": "الرقم كبير جداً"}, status=400)

            note = (request.data.get("note") or "").strip()
            who = getattr(request.user, "username", "") or "staff"
            key = (request.data.get("client_uuid") or "").strip() or str(_uuid.uuid4())
            moved = points_service.adjust(
                store, customer, delta,
                note or f"تعديل يدوي بواسطة {who}",
                key=key,
            )
            invalidate_customers_quick_cache(self.store_id)
            if moved:
                # Told, not silently applied. Points appearing or vanishing
                # with no explanation is how a loyalty scheme loses trust.
                push_service.notify_points(
                    store, customer, moved,
                    points_service.balance_of(customer),
                    note or "تعديل من المقهى",
                )
            out = points_service.totals_for(store, customer)
            out["moved"] = moved
            out["value_ils"] = str(points_service.value_of(out["balance"]))
            return Response(out, status=200)

        out = points_service.totals_for(store, customer)
        out["value_ils"] = str(points_service.value_of(out["balance"]))
        out["points_per_ils"] = points_service.POINTS_PER_ILS
        out["earn_rate"] = str(points_service.EARN_RATE)
        out["activity"] = [
            {
                "delta": r["delta"],
                "reason": r["reason"],
                "note": r["note"],
                "balance_after": r["balance_after"],
                "at": r["created_at"].isoformat(),
            }
            for r in models.BeanLedger.objects.for_pharmacy(self.store_id)
            .filter(customer=customer)
            .order_by("-created_at")[:50]
            .values("delta", "reason", "note", "balance_after", "created_at")
        ]
        return Response(out)

    @action(detail=True, methods=["post"])
    def settle(self, request, pk=None):
        """Bulk debt collection for one customer.

        `POST /api/v1/customers/{id}/settle/`
        - no body / no `amount`  → mark ALL unpaid debts paid.
        - `{"amount": "150.00"}` → apply the payment oldest-debt-first
          (FIFO); the last touched debt may end up partially paid.
        """
        from django.db import transaction

        customer = self.get_object()
        raw = request.data.get("amount")
        stamp = timezone.localdate().isoformat()
        settled = 0
        collected = Decimal("0.00")

        with transaction.atomic():
            debts = list(
                models.Debt.objects.for_pharmacy(self.store_id)
                .select_for_update()
                .filter(customer=customer, is_paid=False)
                .order_by("created_at")
            )
            if raw in (None, ""):
                for d in debts:
                    collected += d.discounted_total
                    d.is_paid = True
                    d.note = f"{d.note}\nتحصيل كامل ({stamp})".strip()
                    d.save(update_fields=["is_paid", "note", "updated_at"])
                    settled += 1
            else:
                try:
                    amount = Decimal(str(raw)).quantize(Decimal("0.01"))
                except Exception:  # noqa: BLE001
                    return Response({"amount": "قيمة غير صالحة"}, status=400)
                if amount <= 0:
                    return Response({"amount": "أدخل مبلغاً أكبر من صفر"}, status=400)
                for d in debts:
                    if amount <= 0:
                        break
                    remaining = d.discounted_total
                    if amount >= remaining:
                        amount -= remaining
                        collected += remaining
                        d.is_paid = True
                        d.note = f"{d.note}\nتحصيل كامل ({stamp})".strip()
                        d.save(update_fields=["is_paid", "note", "updated_at"])
                        settled += 1
                    else:
                        d.discounted_total = remaining - amount
                        d.note = f"{d.note}\nدفعة {amount} ₪ ({stamp})".strip()
                        d.save(
                            update_fields=["discounted_total", "note", "updated_at"]
                        )
                        collected += amount
                        amount = Decimal("0.00")

        invalidate_dashboard_cache(self.store_id)
        invalidate_customers_quick_cache(self.store_id)

        outstanding = models.Debt.objects.for_pharmacy(self.store_id).filter(
            customer=customer, is_paid=False
        ).aggregate(
            s=Coalesce(
                Sum("discounted_total"),
                Decimal("0.00"),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )["s"]
        return Response(
            {
                "settled_count": settled,
                "collected": f"{collected:.2f}",
                "outstanding": f"{outstanding:.2f}",
            }
        )


class DebtViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    """CRUD + search/filter/sort for debts (with their med line items).

    - search:  ?search=  over the customer's name/phone and the note
    - filter:  ?customer=<id> ?is_paid=true|false
    - sort:    ?ordering=-created_at | total | discounted_total ...
    """

    # unscoped() base: StoreScopedMixin re-filters by tenant every request.
    queryset = (
        models.Debt.objects.unscoped()
        .select_related("customer", "created_by")
        .prefetch_related("items")
    )
    serializer_class = serializers.DebtSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleEnabled]
    required_module = "debts"
    filterset_class = DebtFilter
    search_fields = ["customer__name", "customer__phone", "note"]
    ordering_fields = ["total", "discounted_total", "is_paid", "created_at", "updated_at"]
    ordering = ["-created_at"]

    def perform_create(self, serializer):
        user = self.request.user if self.request.user.is_authenticated else None
        serializer.save(created_by=user, store_id=self.store_id)
        invalidate_dashboard_cache(self.store_id)
        invalidate_customers_quick_cache(self.store_id)

    def perform_update(self, serializer):
        serializer.save()
        invalidate_dashboard_cache(self.store_id)
        invalidate_customers_quick_cache(self.store_id)

    def perform_destroy(self, instance):
        """Delete a debt — but never one that belongs to a credit sale.

        `Sale.debt` is SET_NULL, so deleting a sale-linked debt leaves the sale
        still labelled "دين" with nothing behind it: the sales page says the
        customer owes 10, the customer's page says 0, and nobody can tell which
        is true. The sale is the source of truth, so the only correct way to
        cancel that money is to void the sale (which restores stock AND removes
        this debt together). Standalone debts — recorded by hand, not from a
        sale — stay freely deletable, and are logged so "where did it go?" is
        always answerable.
        """
        linked = list(instance.sales.values_list("pk", flat=True))
        if linked:
            raise ValidationError({
                "detail": (
                    "هذا الدين ناتج عن بيع بالدين (بيع رقم "
                    f"{linked[0]}). لإلغائه احذف البيع نفسه من صفحة المبيعات "
                    "— هيك بيرجع المخزون ويتشال الدين مع بعض."
                )
            })

        with transaction.atomic():
            models.AuditLog.objects.create(
                store_id=self.store_id,
                actor=self.request.user if self.request.user.is_authenticated else None,
                action=models.AuditLog.ACTION_DEBT_DELETE,
                summary=(
                    f"دين {instance.discounted_total} — "
                    f"{instance.customer.name if instance.customer_id else ''}"
                ).strip(" —"),
                request={
                    "debt_id": instance.pk,
                    "customer_id": instance.customer_id,
                    "total": str(instance.total),
                    "discounted_total": str(instance.discounted_total),
                    "is_paid": instance.is_paid,
                    "note": instance.note,
                },
                affected=1,
            )
            instance.delete()
        invalidate_dashboard_cache(self.store_id)
        invalidate_customers_quick_cache(self.store_id)

    @action(detail=False, methods=["get"])
    def dashboard(self, request):
        """All home/insights KPIs in ONE cheap, Redis-cached call.

        `GET /api/v1/debts/dashboard/` →
        totals, paid/unpaid counts, customer + gender counts, last-6-months
        series, and the top 6 debtors. Previously the frontend paged through
        every debt and customer to compute this client-side.
        """
        pid = self.store_id
        cached = cache.get(dashboard_key(pid))
        if cached is not None:
            return Response(cached)

        money = DecimalField(max_digits=18, decimal_places=2)
        debts = models.Debt.objects.for_pharmacy(pid)
        agg = debts.aggregate(
            total_outstanding=Coalesce(
                Sum("discounted_total", filter=Q(is_paid=False), output_field=money),
                Decimal("0.00"),
                output_field=money,
            ),
            total_collected=Coalesce(
                Sum("discounted_total", filter=Q(is_paid=True), output_field=money),
                Decimal("0.00"),
                output_field=money,
            ),
            unpaid_count=Count("id", filter=Q(is_paid=False)),
            paid_count=Count("id", filter=Q(is_paid=True)),
        )

        customers = models.Customer.objects.for_pharmacy(pid)
        gender = customers.aggregate(
            male=Count("id", filter=Q(gender="male")),
            female=Count("id", filter=Q(gender="female")),
            total=Count("id"),
        )

        monthly = [
            {
                "month": row["month"].strftime("%Y-%m"),
                "count": row["count"],
                "amount": row["amount"],
            }
            for row in (
                debts.annotate(month=TruncMonth("created_at"))
                .values("month")
                .annotate(
                    count=Count("id"),
                    amount=Coalesce(
                        Sum("discounted_total", output_field=money),
                        Decimal("0.00"),
                        output_field=money,
                    ),
                )
                .order_by("-month")[:6]
            )
        ][::-1]

        top_debtors = [
            {"id": c["id"], "name": c["name"], "amount": c["outstanding"]}
            for c in (
                customers.annotate(
                    outstanding=Coalesce(
                        Sum(
                            "debts__discounted_total",
                            filter=Q(debts__is_paid=False),
                            output_field=money,
                        ),
                        Decimal("0.00"),
                        output_field=money,
                    )
                )
                .filter(outstanding__gt=0)
                .order_by("-outstanding")
                .values("id", "name", "outstanding")[:6]
            )
        ]

        payload = {
            "total_outstanding": agg["total_outstanding"],
            "total_collected": agg["total_collected"],
            "unpaid_count": agg["unpaid_count"],
            "paid_count": agg["paid_count"],
            "customer_count": gender["total"],
            "gender_counts": {"male": gender["male"], "female": gender["female"]},
            "monthly": monthly,
            "top_debtors": top_debtors,
        }
        cache.set(dashboard_key(pid), payload, DASHBOARD_TTL)
        return Response(payload)


class SaleViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    """POS sales: create at checkout, browse history, delete to void.

    - search:  ?search=  over the receipt code printed on the receipt (exact),
                 customer name/phone, note, and the sale's item names +
                 barcodes (so a scanned product barcode finds sales containing
                 that product)
    - filter:  ?customer=<id> ?payment_method=cash|debt ?item=<name|barcode>
    - sort:    ?ordering=-created_at | discounted_total ...
    Deleting a sale restores stock and removes its linked (unpaid) debt.
    """

    # unscoped() base: StoreScopedMixin re-filters by tenant every request.
    queryset = (
        models.Sale.objects.unscoped()
        .select_related("customer", "created_by", "debt")
        .prefetch_related("items")
        # distinct=True because the search filter joins `items`; without it a
        # three-line sale would report three revisions.
        .annotate(
            revision_count_annotated=Count("revisions", distinct=True)
        )
    )
    serializer_class = serializers.SaleSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleEnabled]
    required_module = "pos"
    # PATCH edits a sale in place — same id, same receipt code, same position
    # in the day — keeping every previous version in SaleRevision. PUT stays
    # off: a full replace with no items would be a silent wipe, and the client
    # has no reason to send one.
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filterset_class = SaleFilter
    search_fields = [
        # First, so scanning the barcode printed on a receipt lands straight
        # on that one sale — the whole point of printing it. `=` is an exact
        # match: a 12-digit code must not fuzzy-match a product barcode.
        "=receipt_code",
        "customer__name",
        "customer__phone",
        "note",
        "items__medication_name",
        "items__product__barcode",
    ]
    ordering_fields = ["discounted_total", "created_at"]
    ordering = ["-created_at"]

    def filter_queryset(self, queryset):
        """Make a number in the search box find the sale it belongs to.

        Three things are numbers here and a cashier cannot be expected to know
        which is which:

          * the 12-digit receipt code scanned off the paper — 260819280797
          * the same code TYPED from a smudged receipt, or read off an older
            one that printed the bare id — 145871
          * a product barcode, scanned to find every sale containing that item

        The default SearchFilter handles the first and the third. It misses the
        second, because a backfilled code is the id padded to twelve digits
        (`000000145871`) and an exact match on `145871` finds nothing — which
        is exactly what "I scanned an old sale and it did not work" looks like.

        So a purely numeric term ALSO matches the zero-padded code and the raw
        primary key, unioned with whatever the normal search found. Anything
        non-numeric is left completely alone.
        """
        qs = super().filter_queryset(queryset)
        term = (self.request.query_params.get("search") or "").strip()
        if not term.isdigit() or len(term) > 12:
            return qs

        # Re-run the OTHER backends (tenant filterset, ordering) on the
        # unsearched queryset, so the union is not narrowed by the very search
        # we are trying to widen.
        base = queryset
        for backend in self.filter_backends:
            if issubclass(backend, filters.SearchFilter):
                continue
            base = backend().filter_queryset(self.request, base, self)

        alt = Q(receipt_code=term) | Q(receipt_code=term.zfill(12))
        # A bare id, from a receipt printed before receipt codes existed.
        # Bounded so a 13-digit product barcode can never be read as a pk.
        if len(term) <= 9:
            alt |= Q(pk=int(term))
        return (qs | base.filter(alt)).distinct()

    def _invalidate(self):
        pid = self.store_id
        invalidate_sales_stats_cache(pid)
        invalidate_med_stats_cache(pid)  # stock changed
        invalidate_pos_catalog_cache(pid)  # stock changed
        invalidate_dashboard_cache(pid)  # a credit sale may add a debt
        invalidate_customers_quick_cache(pid)  # outstanding may change

    def perform_create(self, serializer):
        user = self.request.user if self.request.user.is_authenticated else None
        serializer.save(created_by=user, store_id=self.store_id)
        self._invalidate()

    def get_permissions(self):
        perms = super().get_permissions()
        # Bulk delete wipes sales history (and reverses stock) — owners only,
        # never employees. Same rule as MedicationViewSet.bulk_delete.
        if getattr(self, "action", None) == "bulk_delete":
            perms.append(OwnerRequired())
        return perms

    @staticmethod
    def _restores_stock(sale) -> bool:
        """False for sales that never took stock out in the first place.

        The rule itself lives on the model, so voiding, bulk-deleting and
        editing a sale all ask the same question — see Sale.moves_stock.
        """
        return sale.moves_stock()

    def _void_sale(self, instance):
        """Reverse a sale's stock movement + drop its linked unpaid debt, then
        delete it. Shared by single- and bulk-delete; the caller invalidates.

        Atomic and locked: without the lock two concurrent DELETEs (a double
        tap, or a client retry racing the original) both read the sale, both
        credit stock back, and the second delete is a silent no-op — stock ends
        up restored twice. Without the transaction, a worker killed mid-loop
        leaves stock partly restored AND the sale still present, so the retry
        restores it again.
        """
        from django.db.models import F as _F

        pid = self.store_id
        with transaction.atomic():
            locked = (
                models.Sale.objects.for_pharmacy(pid)
                .select_for_update()
                .filter(pk=instance.pk)
                .first()
            )
            if locked is None:
                return  # someone else voided it while we waited for the lock

            if self._restores_stock(locked):
                # sale → put stock back, return → take it out again
                delta = -1 if locked.is_return else 1
                for item in locked.items.all():
                    if item.variant_id:
                        models.ProductVariant.objects.for_pharmacy(pid).filter(
                            pk=item.variant_id
                        ).update(stock=_F("stock") + delta * item.quantity)
                    elif item.product_id:
                        models.Product.objects.for_pharmacy(pid).filter(
                            pk=item.product_id
                        ).update(stock=_F("stock") + delta * item.quantity)
            if locked.debt_id and not locked.debt.is_paid:
                locked.debt.delete()
            locked.delete()

    def _log_void(self, sale, *, bulk_count=None):
        """Leave a trace. Deleting a sale erases the only other record of it."""
        actor = self.request.user if self.request.user.is_authenticated else None
        if bulk_count is not None:
            models.AuditLog.objects.create(
                store_id=self.store_id,
                actor=actor,
                action=models.AuditLog.ACTION_BULK_DELETE,
                summary=f"حذف {bulk_count} فاتورة بيع",
                request={"scope": "sales", "affected": bulk_count},
                affected=bulk_count,
            )
            return
        models.AuditLog.objects.create(
            store_id=self.store_id,
            actor=actor,
            action=models.AuditLog.ACTION_SALE_DELETE,
            # discounted_total is only written by the serializer, so rows
            # created straight through the ORM carry 0.00 — fall back to the
            # line-derived total rather than logging a void worth "0.00".
            summary=f"بيع {sale.discounted_total or sale.total}".strip(),
            request={
                "sale_id": sale.pk,
                "total": str(sale.total),
                "discounted_total": str(sale.discounted_total),
                "payment_method": sale.payment_method,
                "is_return": sale.is_return,
                "created_at": sale.created_at.isoformat() if sale.created_at else None,
                "note": sale.note,
                "items": [
                    {
                        "name": i.medication_name,
                        "quantity": str(i.quantity),
                        "unit_price": str(i.unit_price),
                    }
                    for i in sale.items.all()[:50]
                ],
            },
            affected=1,
        )

    def perform_destroy(self, instance):
        # Log BEFORE the delete — afterwards the row and its items are gone.
        self._log_void(instance)
        self._void_sale(instance)
        self._invalidate()

    def perform_update(self, serializer):
        """Edit a sale in place, and leave a trail that says so.

        The full before-state is written to SaleRevision by the serializer.
        This adds the one-line entry to the audit log, because that is the list
        an owner actually reads — a sale quietly edited from ₪300 to ₪30 should
        surface there next to the voids, not only inside the invoice.
        """
        before = models.SaleRevision.snapshot_of(serializer.instance)
        sale = serializer.save()
        actor = self.request.user if self.request.user.is_authenticated else None
        models.AuditLog.objects.create(
            store_id=self.store_id,
            actor=actor,
            action=models.AuditLog.ACTION_SALE_EDIT,
            summary=(
                f"فاتورة {sale.receipt_code or sale.pk}: "
                f"{before['discounted_total']} ← {sale.discounted_total}"
            ),
            request={
                "sale_id": sale.pk,
                "receipt_code": sale.receipt_code,
                "before": before,
                "after": models.SaleRevision.snapshot_of(sale),
            },
            affected=1,
        )
        self._invalidate()

    @action(
        detail=True,
        methods=["post"],
        url_path=r"revisions/(?P<version>[0-9]+)/restore",
    )
    def restore_revision(self, request, pk=None, version=None):
        """Put the sale back the way it was in one of its earlier versions.

        A restore is an EDIT, not an undo: it runs through the same PATCH path,
        so the version being replaced is itself filed away first. Restoring the
        original and then changing your mind loses nothing — the chain only
        grows. That matters because the alternative, quietly rewinding, would
        erase the very record that makes in-place editing safe to allow.

        A product that has been deleted since is kept as a NAMED line at its
        recorded price rather than failing the whole restore. The money on the
        invoice is what the customer paid and must come back exactly; the
        catalogue link is a convenience, and stock cannot move for a row that
        no longer exists anyway.
        """
        sale = self.get_object()
        pid = self.store_id
        rev = (
            models.SaleRevision.objects.for_pharmacy(pid)
            .filter(sale=sale, version=version)
            .first()
        )
        if rev is None:
            return Response({"detail": "النسخة غير موجودة."}, status=404)

        snap = rev.snapshot or {}
        rows = snap.get("items") or []
        if not rows:
            return Response(
                {"detail": "لا يمكن استرجاع نسخة بدون أصناف."}, status=400
            )

        # Which of the referenced rows still exist, in ONE query each rather
        # than one per line.
        want_products = {r.get("product_id") for r in rows if r.get("product_id")}
        want_variants = {r.get("variant_id") for r in rows if r.get("variant_id")}
        live_products = set(
            models.Product.objects.for_pharmacy(pid)
            .filter(pk__in=want_products)
            .values_list("pk", flat=True)
        )
        live_variants = set(
            models.ProductVariant.objects.for_pharmacy(pid)
            .filter(pk__in=want_variants)
            .values_list("pk", flat=True)
        )

        items = []
        for r in rows:
            item = {
                "quantity": r.get("quantity") or "1",
                "unit_price": r.get("unit_price") or "0",
                # Always sent: it is what a line without a live product falls
                # back to, and it is harmless on one that has it.
                "medication_name": r.get("medication_name") or "صنف",
            }
            if r.get("product_id") in live_products:
                item["product"] = r["product_id"]
            if r.get("variant_id") in live_variants:
                item["variant"] = r["variant_id"]
            items.append(item)

        customer_id = snap.get("customer_id")
        if customer_id and not models.Customer.objects.for_pharmacy(pid).filter(
            pk=customer_id
        ).exists():
            customer_id = None

        payment = snap.get("payment_method") or "cash"
        # A credit sale whose customer is gone cannot be restored as a debt —
        # that would be a balance owed by nobody.
        if payment == "debt" and not customer_id:
            payment = "cash"

        data = {
            "items": items,
            "payment_method": payment,
            "is_return": bool(snap.get("is_return")),
            "customer": customer_id,
            "note": snap.get("note") or "",
        }
        if snap.get("discounted_total") is not None:
            data["discounted_total"] = snap["discounted_total"]

        serializer = self.get_serializer(sale, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    @staticmethod
    def _day_groups(store_id):
        """The shop's own quick-tap groups, as day-summary buckets.

        There is deliberately NO built-in list here. Which lines an owner wants
        totalled at a glance is a fact about his trade — phone credit and
        tobacco in one shop, bread and milk in another — and a default would be
        this template guessing at a business it knows nothing about.

        So it reuses what the shop already told us: the POS quick-tap groups
        (Store.pos_quick_groups). Whatever the owner put on the till's circles
        is what gets its own card. One place to configure, and the till and the
        report can never disagree about what "tobacco" means.
        """
        store = models.Store.objects.filter(pk=store_id).first()
        out = []
        for g in (store.pos_quick_groups if store else None) or []:
            if not isinstance(g, dict):
                continue
            label = str(g.get("label") or "").strip()
            ids = [int(i) for i in (g.get("product_ids") or []) if str(i).isdigit()]
            if label and ids:
                out.append((str(g.get("key") or label), label, ids))
        return out

    @action(detail=False, methods=["get"])
    def day_summary(self, request):
        """Takings for the CURRENT trading day, which does not start at midnight.

        The shop is still selling at 1am and cashes up in the morning, so a
        sale rung at 00:30 belongs to the day that is still going. Splitting it
        by calendar date would put one night's takings in two figures and match
        the drawer in neither. The rollover hour is BUSINESS_DAY_START_HOUR
        (default 4), read in the shop's own timezone.

        `total` is authoritative — it sums what was actually charged, discounts
        included. The group figures sum LINE totals, so on a discounted sale
        they will be a little higher than the share of the total those lines
        represent. That is the honest way round: the owner is asking "how much
        of a group went out today", not "what did it contribute after the
        discount".
        """
        from datetime import datetime, time as dtime

        from django.conf import settings as dj_settings

        pid = self.store_id
        money = DecimalField(max_digits=18, decimal_places=2)
        hour = int(getattr(dj_settings, "BUSINESS_DAY_START_HOUR", 4))

        now = timezone.localtime()
        # Before the rollover, the day that is still running is yesterday's.
        day = now.date() if now.hour >= hour else now.date() - timedelta(days=1)

        def at_cutover(d):
            return timezone.make_aware(
                datetime.combine(d, dtime(hour=hour)), now.tzinfo
            )

        # Every window starts and ends ON the rollover, never at midnight —
        # otherwise "this week" would cut last night's takings in half exactly
        # the way counting by calendar date does.
        period = (request.query_params.get("period") or "day").strip()
        raw_from = (request.query_params.get("from") or "").strip()
        raw_to = (request.query_params.get("to") or "").strip()

        def parse(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                return None

        d_from, d_to = parse(raw_from), parse(raw_to)
        if d_from and d_to:
            if d_to < d_from:
                d_from, d_to = d_to, d_from
            period = "custom"
            start, end = at_cutover(d_from), at_cutover(d_to) + timedelta(days=1)
        elif period == "week":
            # The last seven trading days, today included.
            start, end = at_cutover(day - timedelta(days=6)), at_cutover(day) + timedelta(days=1)
        elif period == "month":
            # Calendar month so far — the same "هذا الشهر" the page already uses.
            start, end = at_cutover(day.replace(day=1)), at_cutover(day) + timedelta(days=1)
        else:
            period = "day"
            start, end = at_cutover(day), at_cutover(day) + timedelta(days=1)

        signed_sale = Case(
            When(is_return=True, then=-F("discounted_total")),
            default=F("discounted_total"),
            output_field=money,
        )
        signed_line = Case(
            When(sale__is_return=True, then=-F("line_total")),
            default=F("line_total"),
            output_field=money,
        )

        sales = models.Sale.objects.for_pharmacy(pid).filter(
            created_at__gte=start, created_at__lt=end
        )
        agg = sales.aggregate(
            amount=Coalesce(
                Sum(signed_sale, output_field=money),
                Decimal("0.00"),
                output_field=money,
            ),
            count=Count("id"),
        )

        items = models.SaleItem.objects.for_pharmacy(pid).filter(
            sale__created_at__gte=start, sale__created_at__lt=end
        )
        groups = []
        for key, label, product_ids in self._day_groups(pid):
            g = items.filter(product_id__in=product_ids).aggregate(
                amount=Coalesce(
                    Sum(signed_line, output_field=money),
                    Decimal("0.00"),
                    output_field=money,
                ),
                # Receipts it appeared on, not lines — "how many customers
                # bought cigarettes" is the question behind the number.
                count=Count("sale_id", distinct=True),
            )
            groups.append(
                {
                    "key": key,
                    "label": label,
                    "amount": g["amount"],
                    "count": g["count"],
                    # Sent so the till can apply the SAME rule to sales it is
                    # still holding in its offline queue.
                    "product_ids": product_ids,
                }
            )

        return Response(
            {
                "day_start": start.isoformat(),
                "day_end": end.isoformat(),
                "period": period,
                "cutover_hour": hour,
                "total": {"amount": agg["amount"], "count": agg["count"]},
                "groups": groups,
            }
        )

    @action(detail=True, methods=["get"])
    def revisions(self, request, pk=None):
        """Every previous version of this sale, newest first.

        The live sale is not repeated here — it is what the detail endpoint
        already returns. `version` 1 is the sale as it was originally rung.
        """
        sale = self.get_object()
        rows = (
            models.SaleRevision.objects.for_pharmacy(self.store_id)
            .filter(sale=sale)
            .select_related("edited_by")
        )
        return Response(
            {
                "results": [
                    {
                        "id": r.pk,
                        "version": r.version,
                        "edited_at": r.created_at.isoformat(),
                        "edited_by": (
                            (r.edited_by.get_full_name() or "").strip()
                            or r.edited_by.get_username()
                        )
                        if r.edited_by_id
                        else "",
                        "snapshot": r.snapshot,
                    }
                    for r in rows
                ]
            }
        )

    @action(detail=False, methods=["post"])
    def bulk_delete(self, request):
        """Owner-only wipe of sales — stock restored, linked unpaid debts removed.
        Body: {"ids": [...]} or {"all": true}. Scoped to the requester's store,
        so foreign ids match nothing.

        Batched: stock reversals are aggregated per product and applied with one
        UPDATE each (F-expression, so still atomic and exact), and debts + sales
        are deleted in bulk. Wiping thousands of imported invoices is a few dozen
        queries instead of tens of thousands — it no longer ties up a worker.
        """
        from collections import defaultdict
        from django.db.models import F as _F

        pid = self.store_id
        qs = (
            models.Sale.objects.for_pharmacy(pid)
            .prefetch_related("items")
            .select_related("debt")
        )
        if not request.data.get("all"):
            ids = request.data.get("ids")
            if not isinstance(ids, list) or not ids:
                return Response(
                    {"detail": "حدّد فواتير للحذف أو أرسل all=true."}, status=400
                )
            qs = qs.filter(id__in=ids)

        med_delta = defaultdict(Decimal)
        var_delta = defaultdict(Decimal)
        sale_ids, debt_ids = [], []
        with transaction.atomic():
            for sale in qs.iterator(chunk_size=1000):
                # sale → put stock back, return → take it out again.
                # Migrated Shamel invoices never took stock out, so crediting
                # them back would invent stock — see _restores_stock.
                if self._restores_stock(sale):
                    delta = -1 if sale.is_return else 1
                    for item in sale.items.all():
                        if item.variant_id:
                            var_delta[item.variant_id] += delta * item.quantity
                        elif item.product_id:
                            med_delta[item.product_id] += delta * item.quantity
                if sale.debt_id and sale.debt and not sale.debt.is_paid:
                    debt_ids.append(sale.debt_id)
                sale_ids.append(sale.id)

            deleted = len(sale_ids)
            for mid, d in med_delta.items():
                if d:
                    models.Product.objects.for_pharmacy(pid).filter(
                        pk=mid
                    ).update(stock=_F("stock") + d)
            for vid, d in var_delta.items():
                if d:
                    models.ProductVariant.objects.for_pharmacy(pid).filter(
                        pk=vid
                    ).update(stock=_F("stock") + d)
            if debt_ids:
                models.Debt.objects.for_pharmacy(pid).filter(id__in=debt_ids).delete()
            if sale_ids:
                self._log_void(None, bulk_count=deleted)
                models.Sale.objects.for_pharmacy(pid).filter(id__in=sale_ids).delete()
        self._invalidate()
        return Response({"deleted": deleted})

    @action(detail=False, methods=["get"])
    def stats(self, request):
        """Sales analytics in one Redis-cached call.

        `GET /api/v1/sales/stats/` → period totals (today/yesterday/7d/this
        month/last month/all time), 30-day category split, 14-day daily
        series, and the 30-day cash-vs-debt split.
        """
        pid = self.store_id
        cached = cache.get(sales_stats_key(pid))
        if cached is not None:
            return Response(cached)

        money = DecimalField(max_digits=18, decimal_places=2)
        now = timezone.localtime()
        today = now.date()
        month_start = today.replace(day=1)
        last_month_end = month_start - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        # Returns count as negative amounts everywhere.
        signed = Case(
            When(is_return=True, then=-F("discounted_total")),
            default=F("discounted_total"),
            output_field=money,
        )
        signed_line = Case(
            When(sale__is_return=True, then=-F("line_total")),
            default=F("line_total"),
            output_field=money,
        )

        def bucket(qs):
            agg = qs.aggregate(
                amount=Coalesce(
                    Sum(signed, output_field=money),
                    Decimal("0.00"),
                    output_field=money,
                ),
                count=Count("id"),
            )
            return {"amount": agg["amount"], "count": agg["count"]}

        sales = models.Sale.objects.for_pharmacy(pid)
        periods = {
            "today": bucket(sales.filter(created_at__date=today)),
            "yesterday": bucket(
                sales.filter(created_at__date=today - timedelta(days=1))
            ),
            "week": bucket(
                sales.filter(created_at__date__gte=today - timedelta(days=6))
            ),
            "month": bucket(sales.filter(created_at__date__gte=month_start)),
            "last_month": bucket(
                sales.filter(
                    created_at__date__gte=last_month_start,
                    created_at__date__lte=last_month_end,
                )
            ),
            "all_time": bucket(sales),
        }

        window = today - timedelta(days=29)
        units = DecimalField(max_digits=18, decimal_places=3)
        signed_qty = Case(
            When(sale__is_return=True, then=-F("quantity")),
            default=F("quantity"),
            output_field=units,
        )
        by_category = [
            {
                "category": row["category"] or "أخرى",
                "amount": row["amount"],
                # How many UNITS of this category were sold (returns negative).
                "qty": row["qty"],
            }
            for row in (
                models.SaleItem.objects.for_pharmacy(pid)
                .filter(sale__created_at__date__gte=window)
                .values("category")
                .annotate(
                    amount=Coalesce(
                        Sum(signed_line, output_field=money),
                        Decimal("0.00"),
                        output_field=money,
                    ),
                    qty=Coalesce(
                        Sum(signed_qty, output_field=units),
                        Decimal("0"),
                        output_field=units,
                    ),
                )
                .order_by("-amount")[:8]
            )
        ]

        daily = [
            {
                "date": row["day"].isoformat(),
                "amount": row["amount"],
                "count": row["count"],
            }
            for row in (
                sales.filter(created_at__date__gte=today - timedelta(days=13))
                .annotate(day=TruncDate("created_at"))
                .values("day")
                .annotate(
                    amount=Coalesce(
                        Sum(signed, output_field=money),
                        Decimal("0.00"),
                        output_field=money,
                    ),
                    count=Count("id"),
                )
                .order_by("day")
            )
        ]

        payment_split = {
            row["payment_method"]: row["amount"]
            for row in (
                sales.filter(created_at__date__gte=window)
                .values("payment_method")
                .annotate(
                    amount=Coalesce(
                        Sum(signed, output_field=money),
                        Decimal("0.00"),
                        output_field=money,
                    )
                )
            )
        }

        payload = {
            "periods": periods,
            "by_category": by_category,
            "daily": daily,
            "payment_split": {
                "cash": payment_split.get("cash", Decimal("0.00")),
                "debt": payment_split.get("debt", Decimal("0.00")),
            },
        }
        cache.set(sales_stats_key(pid), payload, SALES_STATS_TTL)
        return Response(payload)


class _LiteUser:
    """Stand-in user carrying only the id (no DB hit for polling auth)."""

    is_authenticated = True

    def __init__(self, pk):
        self.pk = pk
        self.id = pk


class JWTNoDBAuthentication(JWTAuthentication):
    """JWT auth that trusts the (signed) token's user_id without a DB lookup.

    Used only for the cart-state endpoint so frequent realtime polling never
    touches Postgres — signature validation still guarantees authenticity.
    """

    def get_user(self, validated_token):
        return _LiteUser(validated_token["user_id"])


class PosCartStateView(APIView):
    """Open POS carts synced per account across devices, in near-realtime.

    Reads are served from Redis when a real shared Redis is configured
    (REDIS_URL); Postgres keeps the durable copy and is only written when
    carts actually change. Without Redis the per-process LocMemCache would
    serve each gunicorn worker its own stale copy, so caching is skipped
    entirely and reads go straight to the DB — always coherent.

    DELIBERATELY EXEMPT from the StoreResolved 400 guard: the cart blob is
    a per-USER resource (OneToOne on the account, `_LiteUser` carries no
    store) — it is scoped by account identity, not by tenant.
    """

    authentication_classes = [JWTNoDBAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    CACHE_TTL = 10 * 60  # safety net; writes refresh the key anyway

    @staticmethod
    def _cache_key(user_id):
        return f"store:pos_cart:{user_id}"

    def get(self, request):
        key = self._cache_key(request.user.pk)
        if settings.REDIS_ENABLED:
            cached = cache.get(key)
            if cached is not None:
                return Response(cached)
        state = models.PosCartState.objects.filter(user_id=request.user.pk).first()
        payload = {
            "data": state.data if state else {},
            "updated_at": state.updated_at.isoformat() if state else None,
        }
        if settings.REDIS_ENABLED:
            cache.set(key, payload, self.CACHE_TTL)
        return Response(payload)

    def put(self, request):
        data = request.data.get("data")
        if not isinstance(data, dict):
            return Response({"data": "expected an object"}, status=400)
        state, _ = models.PosCartState.objects.update_or_create(
            user_id=request.user.pk, defaults={"data": data}
        )
        payload = {"data": state.data, "updated_at": state.updated_at.isoformat()}
        if settings.REDIS_ENABLED:
            cache.set(self._cache_key(request.user.pk), payload, self.CACHE_TTL)
        return Response({"updated_at": state.updated_at})


class HesabateImportView(APIView):
    """Guided Hesabate ingestion — dry-run first, atomic commit second.

    POST /api/v1/import/hesabate/products/   file=<xlsx>
    POST /api/v1/import/hesabate/sales/      invoices=<xlsx> items=<xlsx>

    Without `commit=true` the upload is only parsed and validated: the
    response lists every problem with its exact Excel row number and nothing
    is written. With `commit=true` the import runs inside ONE transaction —
    any error rolls the whole thing back. Everything is scoped to the
    requesting user's store and can never touch anyone else's records.
    """

    # OwnerRequired: imports rewrite the whole catalogue — employees can't.
    permission_classes = [
        permissions.IsAuthenticated,
        ModuleEnabled,
        OwnerRequired,
        StoreResolved,  # tenant-API 400 guard
    ]
    required_module = "imports"

    # No hard upload-size or row cap: a store must be able to import its
    # entire catalogue / full sales history in one file. Big files are spooled
    # to a temp file by Django and parsed row-by-row (openpyxl read-only), and
    # DATA_UPLOAD_MAX_MEMORY_SIZE is lifted in settings. If a huge file ever
    # needs a ceiling, add it back here — but the product decision is "import
    # anything".
    kind = "products"  # overridden per URL

    def post(self, request):
        from . import importers

        pid = request_pharmacy_id(request)
        commit = str(request.query_params.get("commit", "")).lower() in ("1", "true")
        try:
            if self.kind == "products":
                f = request.FILES.get("file")
                if not f:
                    return Response({"detail": "أرفق ملف المنتجات (file)."}, status=400)
                rows, errors, warnings = importers.parse_products(f)
                # Optional product-side expiry file (Hesabate «كشف تواريخ
                # الصلاحية») — fills expiry by item code / name. Best-effort.
                expiry_f = request.FILES.get("expiry")
                exp_by_code, exp_by_name = {}, {}
                if expiry_f:
                    try:
                        exp_by_code, exp_by_name = importers.parse_expiry_report(expiry_f)
                    except Exception:  # noqa: BLE001
                        exp_by_code, exp_by_name = {}, {}
                # Rename guard: how many EXISTING products would this file
                # rename? A big number usually means the wrong store's
                # file — surface it loudly before anyone commits.
                codes = [r["barcode"] for r in rows if r["barcode"]]
                existing_names: dict[str, str] = {}
                for i in range(0, len(codes), 1000):
                    existing_names.update(
                        models.Product.objects.for_pharmacy(pid)
                        .filter(barcode__in=codes[i : i + 1000])
                        .values_list("barcode", "name")
                    )
                renames = sum(
                    1
                    for r in rows
                    if r["barcode"]
                    and r["barcode"] in existing_names
                    and importers.norm(existing_names[r["barcode"]]).lower()
                    != importers.norm(r["name"]).lower()
                )
                if renames:
                    warnings.insert(0, {
                        "row": 0,
                        "message": (
                            f"⚠️ {renames} صنف موجود سيتغيّر اسمه بهذا الملف — "
                            "تأكد أن الملف يعود لنفس الصيدلية وأنه أحدث نسخة"
                        ),
                    })
                preview = {
                    "valid_rows": len(rows),
                    "expiry_available": len(exp_by_name),
                    "errors": errors,
                    "warnings": warnings,
                    "renames": renames,
                    "sample": [
                        {k: str(v) if v is not None else "" for k, v in r.items()}
                        for r in rows[:5]
                    ],
                }
                if errors or not commit:
                    status = 400 if (errors and commit) else 200
                    return Response({"committed": False, **preview}, status=status)
                store = models.Store.objects.get(pk=pid)
                stats = importers.run_atomic(importers.import_products, store, rows)
                # Fill product expiry from the optional expiry report (only empty).
                try:
                    stats["expiry_set"] = importers.apply_expiry_by_code_name(
                        store, exp_by_code, exp_by_name
                    )
                except Exception:  # noqa: BLE001
                    stats["expiry_set"] = 0
                invalidate_med_stats_cache(pid)
                invalidate_pos_catalog_cache(pid)
                invalidate_reports_cache(pid)
                return Response({"committed": True, "stats": stats, "warnings": warnings})

            invoices_f = request.FILES.get("invoices")
            items_f = request.FILES.get("items")
            if not invoices_f or not items_f:
                return Response(
                    {"detail": "أرفق ملفَي الفواتير (invoices) والأصناف (items)."},
                    status=400,
                )
            invoices, items, errors, warnings = importers.parse_sales(
                invoices_f, items_f
            )
            # Optional 3rd file: the item-movement report (حركة الأصناف) that
            # carries barcodes. Used ONLY as a {name → barcode} key so sale lines
            # link to the catalogue by barcode instead of by (mismatching) name.
            barcode_f = request.FILES.get("barcode")
            name_barcode = {}
            barcode_expiry = {}
            if barcode_f:
                name_barcode, barcode_warnings = importers.parse_barcode_map(barcode_f)
                warnings = list(warnings) + list(barcode_warnings)
                # The SAME movement file also carries تاريخ الصلاحية — reuse it to
                # fill product expiry dates. Best-effort: a bonus, never blocks.
                try:
                    barcode_f.seek(0)
                    barcode_expiry = importers.parse_expiry_map(barcode_f)
                except Exception:  # noqa: BLE001
                    barcode_expiry = {}
            # Read-only coverage estimate so the dry-run can show the % of lines
            # that will link to a product. Best-effort: never let it block import.
            try:
                coverage = importers.sales_match_stats(pid, items, name_barcode)
            except Exception:  # noqa: BLE001
                coverage = None
            preview = {
                "valid_rows": len(invoices),
                "item_rows": sum(len(v) for v in items.values()),
                "barcode_pairs": len(name_barcode),
                "expiry_available": len(barcode_expiry),
                "coverage": coverage,
                "errors": errors,
                "warnings": warnings,
            }
            if errors or not commit:
                status = 400 if (errors and commit) else 200
                return Response({"committed": False, **preview}, status=status)
            store = models.Store.objects.get(pk=pid)
            stats = importers.run_atomic(
                importers.import_sales, store, invoices, items, name_barcode
            )
            # Fill product expiry from the movement file (only where empty).
            try:
                stats["expiry_set"] = importers.apply_expiry(store, barcode_expiry)
            except Exception:  # noqa: BLE001
                stats["expiry_set"] = 0
            invalidate_sales_stats_cache(pid)
            invalidate_dashboard_cache(pid)
            invalidate_med_stats_cache(pid)  # expiry filled → refresh insights
            invalidate_reports_cache(pid)
            return Response({"committed": True, "stats": stats, "warnings": warnings})
        except Exception as exc:  # noqa: BLE001
            from .importers import ImportProblem

            if isinstance(exc, ImportProblem):
                return Response({"detail": str(exc)}, status=400)
            raise


class HesabateImportProductsView(HesabateImportView):
    kind = "products"


class HesabateImportSalesView(HesabateImportView):
    kind = "sales"


class StaffViewSet(viewsets.ModelViewSet):
    """Owner-managed staff for ONE store — /api/v1/staff/.

    Owner-only (OwnerRequired) and tenant-scoped BY HAND: accounts.User has no
    TenantManager, so every read filters on the requester's store and every
    write stamps it — a user id from another store 404s. No hard delete:
    accounts are DEACTIVATED (is_active=False), never removed, so the
    created_by trail on sales stays intact. Deliberately NOT mounted under
    /api/v1/auth/ (that prefix is exempt from the tenant 400 guard); it lives on
    the store router so StoreResolved applies.
    """

    permission_classes = [permissions.IsAuthenticated, OwnerRequired, StoreResolved]
    # No PUT (full replace) and no DELETE (hard delete) — edits are PATCH,
    # removal is a soft is_active=False toggle.
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self):
        from apps.accounts.serializers import StaffSerializer

        return StaffSerializer

    def get_queryset(self):
        from apps.accounts.models import User

        pid = request_pharmacy_id(self.request)
        return User.objects.filter(store_id=pid).order_by("-role", "username")

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        # get_object() runs through get_queryset() → a foreign user id 404s.
        user = self.get_object()
        password = str(request.data.get("password") or "")
        if len(password) < 4:
            return Response(
                {"password": "كلمة المرور قصيرة جداً (4 أحرف على الأقل)."},
                status=400,
            )
        user.set_password(password)
        user.save(update_fields=["password"])
        return Response({"status": "ok"})


class QuickGroupsView(APIView):
    """The POS's quick-tap cards for the requester's store.

    GET  /api/v1/store/quick-groups/   -> {"groups": [...]}
    PUT  /api/v1/store/quick-groups/   {"groups": [...]}

    Stored on the store, not in the browser: this is how the shop works, not a
    preference of one machine. A cleared cache or a second till must not lose
    it.

    A group is {"key", "label", "icon", "product_ids"}. Product ids are NOT
    validated against the catalogue on write — a product deleted later simply
    stops appearing, which is better than refusing to save the whole layout
    because of one stale id.
    """

    permission_classes = [permissions.IsAuthenticated, StoreResolved]

    MAX_GROUPS = 12
    MAX_PRODUCTS = 60

    def get(self, request):
        pid = request_pharmacy_id(request)
        store = models.Store.objects.get(pk=pid)
        return Response({"groups": store.pos_quick_groups or []})

    def put(self, request):
        # Any cashier can rearrange these; they carry no money and no
        # permissions. Locking it to the owner would mean a phone call every
        # time the shop rearranges its own counter.
        pid = request_pharmacy_id(request)
        groups = request.data.get("groups")
        if not isinstance(groups, list):
            return Response({"groups": "قائمة غير صالحة."}, status=400)
        if len(groups) > self.MAX_GROUPS:
            return Response({"groups": "عدد المجموعات كبير جداً."}, status=400)

        clean = []
        for g in groups:
            if not isinstance(g, dict):
                return Response({"groups": "عنصر غير صالح."}, status=400)
            label = str(g.get("label") or "").strip()[:40]
            if not label:
                return Response({"groups": "كل مجموعة تحتاج اسماً."}, status=400)
            ids = g.get("product_ids") or []
            if not isinstance(ids, list) or len(ids) > self.MAX_PRODUCTS:
                return Response({"groups": "قائمة الأصناف غير صالحة."}, status=400)
            clean.append({
                "key": str(g.get("key") or label)[:40],
                "label": label,
                "icon": str(g.get("icon") or "")[:24],
                # Ints only, de-duplicated, order preserved.
                "product_ids": list(dict.fromkeys(
                    int(i) for i in ids if str(i).lstrip("-").isdigit()
                )),
            })

        models.Store.objects.filter(pk=pid).update(pos_quick_groups=clean)
        return Response({"groups": clean})


class PharmacyBrandingView(APIView):
    """Owner-only branding for the requester's store — logo + display name.

    PATCH /api/v1/store/branding/  (multipart/form-data)
      logo_file=<image>   optional — stored to B2, kept as a b2:// marker
      name=<str>          optional — store display name

    Tenant identity comes ONLY from request.user (request_pharmacy_id) — the
    client never names the store. Mirrors the Django-admin logo flow,
    including clearing the public-branding cache so the change shows at once.
    """

    permission_classes = [
        permissions.IsAuthenticated,
        OwnerRequired,
        StoreResolved,
    ]

    def patch(self, request):
        from apps.core.uploads import resolve_stored_url, store_upload

        pid = request_pharmacy_id(request)
        store = models.Store.objects.get(pk=pid)

        changed = []
        name = request.data.get("name")
        if name is not None:
            name = str(name).strip()
            if not name:
                return Response({"name": "الاسم لا يمكن أن يكون فارغاً."}, status=400)
            if len(name) > 255:
                return Response({"name": "الاسم طويل جداً."}, status=400)
            store.name = name
            changed.append("name")

        logo_file = request.FILES.get("logo_file")
        if logo_file is not None:
            try:
                store.logo = store_upload(logo_file, "logos", request)
            except Exception:  # noqa: BLE001 — storage hiccup, surface as 400 not 500
                return Response(
                    {"logo_file": "تعذّر حفظ الشعار، حاول مجدداً."}, status=400
                )
            changed.append("logo")

        # Store-wide default for the near-expiry alert window (days).
        alert_days = request.data.get("expiry_alert_days")
        if alert_days is not None and str(alert_days) != "":
            try:
                alert_days = int(alert_days)
            except (TypeError, ValueError):
                return Response({"expiry_alert_days": "قيمة غير صالحة."}, status=400)
            if not (1 <= alert_days <= 3650):
                return Response(
                    {"expiry_alert_days": "أدخل عدد أيام بين 1 و 3650."}, status=400
                )
            store.expiry_alert_days = alert_days
            changed.append("expiry_alert_days")

        if not changed:
            return Response(
                {"detail": "أرفق شعاراً (logo_file) أو اسماً (name)."}, status=400
            )

        store.save(update_fields=[*changed, "updated_at"])
        # Same cache key the admin form clears — makes the new logo/name appear
        # without waiting out the 10-minute public-branding cache.
        cache.delete(f"store:branding:v1:{store.slug}")
        # A changed alert window shifts every near-expiry count/badge.
        if "expiry_alert_days" in changed:
            invalidate_med_stats_cache(pid)
        return Response(
            {
                "name": store.name,
                "logo": resolve_stored_url(store.logo),
                "expiry_alert_days": store.expiry_alert_days,
            }
        )


class ReportsRestockQuotaView(ReportsBaseView):
    """Owner-only purchase quota — what to restock, rough buy cost, projected
    gain. Gated by the `inventory` module (every store has it) instead of the
    paid `reports` tier, so purchasing works for any owner.

    GET /api/v1/reports/restock-quota/?days=30&cover_days=30&low_stock_threshold=5
    """

    required_module = "purchases"

    def get(self, request):
        from apps.store import reports

        days = self._int(request.query_params.get("days"), 30, 7, 365)
        cover = self._int(request.query_params.get("cover_days"), 30, 7, 365)
        threshold = self._int(
            request.query_params.get("low_stock_threshold"), 5, 0, 100000
        )
        data = reports.restock_quota(
            self.store_id,
            days=days,
            cover_days=cover,
            low_stock_threshold=Decimal(threshold),
        )
        return Response(data)


class ReportsScansView(ReportsBaseView):
    """Owner-only price-check scan analytics — the Reports "تقارير المسح"
    section. Its OWN paid module ("scan_reports"), separate from the inventory
    reports tier, so it can be sold / toggled per store independently.

    GET /api/v1/reports/scans/?days=30 — cached 60s per (store, days).
    DELETE /api/v1/reports/scans/ — owner wipes ALL scan analytics.
    """

    required_module = "scan_reports"
    TTL = 60

    def get(self, request):
        from apps.store import reports

        days = self._int(request.query_params.get("days"), 30, 1, 365)
        key = f"reports:scans:v1:{self.store_id}:{days}"
        data = cache.get(key)
        if data is None:
            data = reports.scans(self.store_id, days=days)
            cache.set(key, data, self.TTL)
        return Response(data)

    def delete(self, request):
        """Owner clears ALL price-scan analytics for the store: the stored
        rows, the live Redis counters (so nothing re-appears on the next flush),
        and the cached report payloads."""
        pid = self.store_id
        deleted, _ = models.ScanDaily.objects.for_pharmacy(pid).delete()
        scan_tracking.clear_pharmacy(pid)
        for d in (7, 30, 90):
            cache.delete(f"reports:scans:v1:{pid}:{d}")
        return Response({"deleted": deleted})


class PurchaseOrderViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    """Owner-only purchase orders — draft, receive (raises stock + refreshes
    cost), and history. Tenant-scoped via StoreScopedMixin."""

    queryset = (
        models.PurchaseOrder.objects.unscoped()
        .select_related("created_by")
        .prefetch_related("items")
    )
    serializer_class = serializers.PurchaseOrderSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleEnabled, OwnerRequired]
    required_module = "purchases"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    ordering = ["-created_at"]

    def perform_create(self, serializer):
        user = self.request.user if self.request.user.is_authenticated else None
        serializer.save(store_id=self.store_id, created_by=user)

    def perform_destroy(self, instance):
        from django.db.models import F as _F

        # Deleting a RECEIVED order reverses the stock it added. Drafts never
        # touched stock, so a plain delete is enough.
        if instance.status == "received":
            with transaction.atomic():
                for item in instance.items.all():
                    if item.product_id:
                        models.Product.objects.for_pharmacy(
                            self.store_id
                        ).filter(pk=item.product_id).update(
                            stock=_F("stock") - item.quantity
                        )
                instance.delete()
            invalidate_med_stats_cache(self.store_id)
            invalidate_pos_catalog_cache(self.store_id)
        else:
            instance.delete()

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        from django.db.models import F as _F
        from django.utils import timezone as _tz

        order = self.get_object()
        if order.status == "received":
            return Response({"detail": "الطلبية مستلمة مسبقاً."}, status=400)
        with transaction.atomic():
            for item in order.items.all():
                if not item.product_id:
                    continue
                med_qs = models.Product.objects.for_pharmacy(
                    self.store_id
                ).filter(pk=item.product_id)
                if item.unit_cost and item.unit_cost > 0:
                    med_qs.update(
                        stock=_F("stock") + item.quantity, cost=item.unit_cost
                    )
                else:
                    med_qs.update(stock=_F("stock") + item.quantity)
            order.status = "received"
            order.received_at = _tz.now()
            order.save(update_fields=["status", "received_at", "updated_at"])
        invalidate_med_stats_cache(self.store_id)
        invalidate_pos_catalog_cache(self.store_id)
        return Response(self.get_serializer(order).data)



class ShopMeView(APIView):
    """The signed-in customer's own standing, for the PWA home screen.

    Until this existed the app rendered `const START_BEANS = 248` — a literal
    — next to three more literals for cups, free drinks and streak. It looked
    finished and told every customer the same lie. Everything here is read
    from the rows that already exist: LoyaltyProfile for the cached balance
    and tier, BeanLedger for what actually happened, Sale for the count.

    Authenticated by Clerk, not by the staff JWT: the customer app has no
    staff session and must never be given one.
    """

    authentication_classes = [FirebaseAuthentication, ClerkAuthentication]
    #: NOT IsAuthenticated — a customer has no Django user. See IsAppCustomer.
    permission_classes = [IsAppCustomer]

    #: How many beans a free drink costs. A constant for now — when the shop
    #: wants to tune it, it moves onto Store and this becomes a lookup.
    #: Kept as attributes only because other code still reads them off this
    #: class. The maths itself lives in apps.store.points — one module, so the
    #: till, the app and the receipt can never quote three different rates.
    POINTS_PER_ILS = points_service.POINTS_PER_ILS
    EARN_RATE = points_service.EARN_RATE

    def get(self, request):
        ident = identity_filter(request)
        if not ident:
            return Response({"detail": "سجّل دخولك للمتابعة"}, status=401)

        store = models.Store.objects.filter(
            slug=getattr(settings, "CLERK_STORE_SLUG", "koup")
        ).first()
        if store is None:
            return Response({"detail": "المقهى غير متاح حالياً"}, status=503)

        customer = (
            models.Customer.objects.for_pharmacy(store.pk)
            .filter(**ident)
            .select_related("loyalty")
            .first()
        )
        if customer is None:
            # Signed in with Clerk but never synced. The app calls
            # /clerk/sync/ on launch, so this is a first-run race, not an
            # error worth showing anyone.
            return Response({"synced": False, "beans": 0}, status=200)

        profile = getattr(customer, "loyalty", None)
        beans = getattr(profile, "beans", 0) or 0

        year_start = timezone.now().replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        cups_this_year = (
            models.Sale.objects.for_pharmacy(store.pk)
            .filter(customer=customer, is_return=False, created_at__gte=year_start)
            .count()
        )
        # How many times they have spent points. Not "free cups" any more —
        # points are cash value now, and a redemption may be part of a bill.
        redemptions = (
            models.BeanLedger.objects.for_pharmacy(store.pk)
            .filter(customer=customer, reason=models.BeanLedger.Reason.REDEEM)
            .count()
        )
        earned_total = (
            models.BeanLedger.objects.for_pharmacy(store.pk)
            .filter(customer=customer, delta__gt=0)
            .aggregate(n=Coalesce(Sum("delta"), 0))["n"]
        )
        # Reported positive. "You have used 340 points" is the sentence; a
        # minus sign in front of it makes the customer read it as a penalty.
        spent_total = -(
            models.BeanLedger.objects.for_pharmacy(store.pk)
            .filter(customer=customer, delta__lt=0)
            .aggregate(n=Coalesce(Sum("delta"), 0))["n"]
        )

        recent = list(
            models.BeanLedger.objects.for_pharmacy(store.pk)
            .filter(customer=customer)
            .order_by("-created_at")[:20]
            .values("delta", "reason", "note", "balance_after", "created_at")
        )

        return Response({
            "synced": True,
            "name": customer.name,
            "beans": beans,
            "tier": getattr(profile, "tier", "single"),
            "multiplier": str(getattr(profile, "multiplier", 1)),
            "streak_weeks": getattr(profile, "streak_weeks", 0) or 0,
            "visits_this_month": getattr(profile, "visits_this_month", 0) or 0,
            "cups_this_year": cups_this_year,
            "redemptions": redemptions,
            "points_earned_total": earned_total,
            "points_spent_total": spent_total,
            # The whole scheme, sent to the phone rather than hard-coded in it:
            # the balance's worth in shekels, and the two rates behind it. When
            # the shop changes the rate, the app changes with it.
            "value_ils": str(points_service.value_of(beans)),
            "points_per_ils": points_service.POINTS_PER_ILS,
            "earn_rate": str(points_service.EARN_RATE),
            "activity": [
                {
                    "delta": r["delta"],
                    "reason": r["reason"],
                    "note": r["note"],
                    "balance_after": r["balance_after"],
                    "at": r["created_at"].isoformat(),
                }
                for r in recent
            ],
        })



class ShopOrdersView(APIView):
    """The customer's own orders — place one, and see the ones before it.

    Clerk-authenticated, and scoped to the caller's own customer row: there is
    no order id in the URL and no way to ask for anybody else's. The store slug
    comes from settings, never from the request, for the same reason.
    """

    authentication_classes = [FirebaseAuthentication, ClerkAuthentication]
    #: NOT IsAuthenticated — a customer has no Django user. See IsAppCustomer.
    permission_classes = [IsAppCustomer]

    def _resolve(self, request):
        ident = identity_filter(request)
        if not ident:
            return None, None
        store = models.Store.objects.filter(
            slug=getattr(settings, "CLERK_STORE_SLUG", "koup")
        ).first()
        if store is None:
            return None, None
        customer = (
            models.Customer.objects.for_pharmacy(store.pk)
            .filter(**ident)
            .first()
        )
        return store, customer

    def get(self, request):
        store, customer = self._resolve(request)
        if customer is None:
            return Response({"results": []})
        qs = (
            models.Order.objects.for_pharmacy(store.pk)
            .filter(customer=customer)
            .prefetch_related("items")
            .order_by("-created_at")[:50]
        )
        return Response({"results": serializers.OrderSerializer(qs, many=True).data})

    @transaction.atomic
    def post(self, request):
        store, customer = self._resolve(request)
        if customer is None:
            return Response({"detail": "حسابك لسه ما اكتمل. أعد تسجيل الدخول"}, status=409)

        # Idempotency: a phone on a bad connection retries, and must not order
        # twice. Same contract the offline POS uses for sales.
        client_uuid = (request.data.get("client_uuid") or "").strip() or None
        if client_uuid:
            existing = (
                models.Order.objects.for_pharmacy(store.pk)
                .filter(client_uuid=client_uuid)
                .prefetch_related("items")
                .first()
            )
            if existing is not None:
                return Response(serializers.OrderSerializer(existing).data, status=200)

        fulfilment = (request.data.get("fulfilment") or "pickup").strip()
        if fulfilment not in dict(models.Order.Fulfilment.choices):
            fulfilment = models.Order.Fulfilment.PICKUP
        table = (request.data.get("table_number") or "").strip()[:32]
        if fulfilment != models.Order.Fulfilment.DINE_IN:
            table = ""

        ser = serializers.OrderSerializer(data={
            "items": request.data.get("items") or [],
            "note": request.data.get("note") or "",
            "fulfilment": fulfilment,
            "table_number": table,
            "client_uuid": client_uuid,
        })
        ser.is_valid(raise_exception=True)
        order = ser.save(store=store, customer=customer)

        # ── paying with points ───────────────────────────────────────────
        # Checked HERE, after the total is known and inside the same
        # transaction, because a balance read on the phone thirty seconds ago
        # is not a balance. The ledger row is the redemption; Order.beans_spent
        # is a copy for reprinting a receipt.
        want = request.data.get("beans_spent")
        if want:
            try:
                want = max(0, int(want))
            except (TypeError, ValueError):
                want = 0
        if want:
            # Clamped server-side against the LIVE balance and the bill: the
            # number the phone sent was true when the slider moved, which may
            # have been several minutes and one counter visit ago.
            spend = points_service.spend_on_purchase(
                store, customer, want, order.total,
                source="طلب", source_id=order.pk,
            )
            if spend > 0:
                order.beans_spent = spend
                order.save(update_fields=["beans_spent"])
                push_service.notify_points(
                    store, customer, -spend,
                    points_service.balance_of(customer),
                    f"استُبدلت في الطلب #{order.pk}",
                )

        push_service.notify_order_status(order)
        return Response(serializers.OrderSerializer(order).data, status=201)


class ShopDeviceView(APIView):
    """POST /shop/devices/ — this phone can receive push.

    The app calls this on every launch, not only the first, because FCM tokens
    rotate on their own schedule: after a reinstall, a restore onto a new
    handset, or for no visible reason at all. Registering once at install time
    produces an app that silently stops receiving notifications weeks later,
    which is close to impossible to diagnose from a bug report.

    Upsert on the token, and re-point it at whoever is signed in now — a shared
    family phone must not keep pushing one person's order to the other.
    """

    authentication_classes = [FirebaseAuthentication, ClerkAuthentication]
    permission_classes = [IsAppCustomer]

    def post(self, request):
        ident = identity_filter(request)
        if not ident:
            return Response({"detail": "سجّل دخولك للمتابعة"}, status=401)
        store = models.Store.objects.filter(
            slug=getattr(settings, "CLERK_STORE_SLUG", "koup")
        ).first()
        if store is None:
            return Response({"detail": "المقهى غير متاح حالياً"}, status=503)
        customer = (
            models.Customer.objects.for_pharmacy(store.pk).filter(**ident).first()
        )
        if customer is None:
            return Response({"detail": "حسابك لسه ما اكتمل. أعد تسجيل الدخول"}, status=409)

        token = (request.data.get("token") or "").strip()
        if not token:
            return Response({"detail": "token مطلوب"}, status=400)
        platform = (request.data.get("platform") or "android").strip()
        if platform not in dict(models.DeviceToken.Platform.choices):
            platform = models.DeviceToken.Platform.ANDROID

        models.DeviceToken.objects.unscoped().update_or_create(
            token=token,
            defaults={
                "store": store,
                "customer": customer,
                "platform": platform,
                "device_name": (request.data.get("device_name") or "")[:120],
                "app_version": (request.data.get("app_version") or "")[:40],
                "last_seen_at": timezone.now(),
            },
        )
        return Response({"ok": True})

    def delete(self, request):
        """Sign-out. The token must stop resolving to this customer at once."""
        token = (request.data.get("token") or "").strip()
        if token:
            models.DeviceToken.objects.unscoped().filter(token=token).delete()
        return Response({"ok": True})


class ShopOrderCancelView(APIView):
    """POST /shop/orders/<pk>/cancel/ — the customer changed their mind.

    Only while the order is still `placed`. Once the counter has accepted it
    somebody has started making the drink, and cancelling then is a
    conversation with a barista, not an API call.

    Refunding the beans is not optional: they were spent inside the order's
    transaction, so an order that never happens must not cost anything. The
    ledger key makes a double-tap on a bad connection idempotent.
    """

    authentication_classes = [FirebaseAuthentication, ClerkAuthentication]
    permission_classes = [IsAppCustomer]

    @transaction.atomic
    def post(self, request, pk=None):
        ident = identity_filter(request)
        if not ident:
            return Response({"detail": "سجّل دخولك للمتابعة"}, status=401)
        store = models.Store.objects.filter(
            slug=getattr(settings, "CLERK_STORE_SLUG", "koup")
        ).first()
        if store is None:
            return Response({"detail": "المقهى غير متاح حالياً"}, status=503)
        customer = (
            models.Customer.objects.for_pharmacy(store.pk).filter(**ident).first()
        )
        if customer is None:
            return Response({"detail": "حسابك لسه ما اكتمل. أعد تسجيل الدخول"}, status=409)

        # Scoped to their own orders: there is no way to cancel anyone else's.
        order = (
            models.Order.objects.for_pharmacy(store.pk)
            .select_for_update()
            .filter(pk=pk, customer=customer)
            .first()
        )
        if order is None:
            return Response({"detail": "الطلب غير موجود"}, status=404)
        if order.status != models.Order.Status.PLACED:
            return Response(
                {"detail": "لا يمكن إلغاء الطلب بعد قبوله، راجع الكاونتر"},
                status=409,
            )

        order.status = models.Order.Status.CANCELLED
        order.cancelled_reason = "أُلغي من التطبيق"
        order.save(update_fields=["status", "cancelled_reason", "updated_at"])

        points_service.refund_spend(
            store, customer, int(order.beans_spent or 0),
            source="طلب", source_id=order.pk,
        )

        push_service.notify_order_status(order)
        return Response(serializers.OrderSerializer(order).data)


class ShopUsualView(APIView):
    """GET /shop/usual/ — what they always order, for one-tap reordering.

    The regular's whole relationship with a coffee shop is that nobody has to
    ask. Most-frequent beats most-recent here: someone who buys the same
    macchiato daily and tried one iced tea last Friday wants the macchiato.
    """

    authentication_classes = [FirebaseAuthentication, ClerkAuthentication]
    permission_classes = [IsAppCustomer]

    def get(self, request):
        ident = identity_filter(request)
        if not ident:
            return Response({"detail": "سجّل دخولك للمتابعة"}, status=401)
        store = models.Store.objects.filter(
            slug=getattr(settings, "CLERK_STORE_SLUG", "koup")
        ).first()
        if store is None:
            return Response({"results": []})
        customer = (
            models.Customer.objects.for_pharmacy(store.pk).filter(**ident).first()
        )
        if customer is None:
            return Response({"results": []})

        rows = (
            models.OrderItem.objects.unscoped()
            .filter(
                order__store_id=store.pk,
                order__customer=customer,
                order__status=models.Order.Status.COLLECTED,
                product__isnull=False,
            )
            .values("product_id", "variant_id", "name")
            .annotate(times=Count("id"))
            .order_by("-times")[:3]
        )
        return Response({"results": list(rows)})


class ShopNotificationsView(APIView):
    """GET the in-app feed; POST to mark read.

    This is the durable half of notifications. Push is the doorbell; this is
    the letterbox, and it still works for everyone who declined permission,
    turned their phone off, or was in a lift.
    """

    authentication_classes = [FirebaseAuthentication, ClerkAuthentication]
    permission_classes = [IsAppCustomer]

    def _resolve(self, request):
        ident = identity_filter(request)
        if not ident:
            return None, None
        store = models.Store.objects.filter(
            slug=getattr(settings, "CLERK_STORE_SLUG", "koup")
        ).first()
        if store is None:
            return None, None
        return store, (
            models.Customer.objects.for_pharmacy(store.pk).filter(**ident).first()
        )

    def get(self, request):
        store, customer = self._resolve(request)
        if customer is None:
            return Response({"results": [], "unread": 0})
        qs = (
            models.Notification.objects.for_pharmacy(store.pk)
            .filter(customer=customer)
            .order_by("-created_at")[:60]
        )
        rows = [
            {
                "id": n.pk,
                "kind": n.kind,
                "title": n.title,
                "body": n.body,
                "data": n.data,
                "read": n.read_at is not None,
                "created_at": n.created_at,
            }
            for n in qs
        ]
        return Response({
            "results": rows,
            "unread": sum(1 for r in rows if not r["read"]),
        })

    def post(self, request):
        """Mark read. No id = mark everything, which is what closing the
        notification sheet means."""
        store, customer = self._resolve(request)
        if customer is None:
            return Response({"ok": True})
        qs = models.Notification.objects.for_pharmacy(store.pk).filter(
            customer=customer, read_at__isnull=True
        )
        ids = request.data.get("ids")
        if ids:
            qs = qs.filter(pk__in=[int(i) for i in ids if str(i).isdigit()])
        qs.update(read_at=timezone.now())
        return Response({"ok": True})


class OrderViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    """Staff side: the counter's queue.

    Read and advance only — an order is created by a customer, never by the
    till, so there is no create here. The status move goes through the model's
    transition table so nothing can walk an order backwards.
    """

    serializer_class = serializers.OrderSerializer
    # StoreScopedMixin supplies `store_id` and appends the StoreResolved guard.
    # Without it `self.store_id` does not exist and every list 500s with an
    # AttributeError — which is exactly what the queue was doing.
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [
        django_filters.rest_framework.DjangoFilterBackend,
        filters.OrderingFilter,
        filters.SearchFilter,
    ]
    # ?customer= is what a customer profile needs; ?status= is what the queue
    # needs. Without a filter backend both were silently ignored and every
    # caller got the whole store's orders.
    filterset_fields = ["status", "customer", "fulfilment"]
    # Searching a reverse relation multiplies rows; distinct() in the queryset
    # keeps one card per order.
    search_fields = ["customer__name", "items__name"]
    ordering = ["-created_at"]
    # POST has to be here or DRF rejects the request in dispatch(), BEFORE it
    # ever looks at the router: `advance` is a POST action, and leaving "post"
    # out of this list 405'd every attempt to accept an order. The list was
    # meant to say "the till cannot CREATE an order" — which is true and still
    # enforced, one method down, where it can be said precisely instead of by
    # blocking a verb the viewset genuinely needs.
    http_method_names = ["get", "post", "patch", "head", "options"]

    def create(self, request, *args, **kwargs):
        return Response(
            {"detail": "الطلب يُنشأ من التطبيق، لا من الكاونتر"},
            status=405,
        )

    def get_queryset(self):
        return (
            models.Order.objects.for_pharmacy(self.store_id)
            .select_related("customer")
            .prefetch_related("items")
            .distinct()
        )

    #: The four states that mean somebody is waiting. Everything else is
    #: finished business and belongs in history.
    OPEN_STATUSES = (
        models.Order.Status.PLACED,
        models.Order.Status.ACCEPTED,
        models.Order.Status.PREPARING,
        models.Order.Status.READY,
    )

    @action(detail=False, methods=["get"])
    def live(self, request):
        """GET /orders/live/ — the counter's board, and only the board.

        Polled from every admin page (that is what the sidebar badge is), so it
        has to be cheap: one query on the (store, status, created_at) index,
        oldest first, no pagination and no count query. The alternative the
        admin used before this — fetch fifty orders of any age and filter them
        in the browser — grew with the shop's history and got slower every week
        it ran.

        `pending` is the number nobody has ACCEPTED yet. That, not the length of
        the list, is the number worth putting on a badge: an order being made is
        already someone's job; an order nobody has looked at is not.
        """
        qs = (
            self.get_queryset()
            .filter(status__in=self.OPEN_STATUSES)
            .order_by("created_at")
        )
        rows = list(qs)

        # Today's finished orders ride along so a mis-click is recoverable
        # from the board. Marking the wrong cup collected is the easiest
        # mistake to make here and, until now, the only one that needed a
        # database query to undo.
        start_of_day = timezone.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        recent = list(
            self.get_queryset()
            .filter(
                status__in=(
                    models.Order.Status.COLLECTED,
                    models.Order.Status.CANCELLED,
                ),
                updated_at__gte=start_of_day,
            )
            .order_by("-updated_at")[:12]
        )

        return Response({
            "results": serializers.OrderSerializer(rows, many=True).data,
            "recent": serializers.OrderSerializer(recent, many=True).data,
            "pending": sum(
                1 for o in rows if o.status == models.Order.Status.PLACED
            ),
            "open": len(rows),
        })

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def advance(self, request, pk=None):
        """POST {status} — put this order in that state. Any state.

        The ladder is gone (see Order.TRANSITIONS). What replaces it is not
        "no rules" but a different KIND of rule: instead of asking whether a
        move is allowed, this asks what the money should look like once the
        move has happened, and makes it look like that.

        Two states have consequences, and both are now symmetric:

          collected  — the order becomes a Sale: takings, reports, stock.
                       Entering it creates one; LEAVING it voids that one and
                       takes the earned points back.
          cancelled  — points the customer spent are given back. Leaving it
                       charges them again, because the order is live once more.

        Every movement is keyed with a cycle number, so an order can go round
        that loop as many times as a busy counter needs it to and the balance
        lands where it should. The row is locked for the whole thing: two
        screens can be showing the same card.
        """
        from apps.store.fulfil import sale_for_order, void_sale_for_order

        order = self.get_object()
        # Re-read under lock. get_object() read it a moment ago and this method
        # both branches on and rewrites the status.
        # of=("self",) is load-bearing on Postgres. `sale` is a NULLABLE FK, so
        # select_related() joins it as a LEFT OUTER JOIN, and Postgres refuses
        # to lock the nullable side of an outer join:
        #   "FOR UPDATE cannot be applied to the nullable side of an outer join"
        # Naming `self` locks the order row and nothing else, which is all this
        # ever wanted — the sale and the customer are read, not written, here.
        order = (
            models.Order.objects.unscoped()
            .select_for_update(of=("self",))
            .select_related("customer", "store", "sale")
            .get(pk=order.pk)
        )

        target = (request.data.get("status") or "").strip()
        if target not in models.Order.Status.values:
            return Response({"detail": "حالة غير معروفة"}, status=400)

        previous = order.status
        if target == previous:
            # A retry, or a card dropped back where it came from. Not an error,
            # and — importantly — not an event: nothing may move twice because
            # somebody's connection was slow.
            return Response(self.get_serializer(order).data)

        if not order.can_move_to(target):
            return Response(
                {"detail": f"لا يمكن الانتقال من {previous} إلى {target}"},
                status=400,
            )

        COLLECTED = models.Order.Status.COLLECTED
        CANCELLED = models.Order.Status.CANCELLED
        spent = int(order.beans_spent or 0)

        order.status = target
        fields = ["status", "updated_at"]
        if target == CANCELLED:
            reason = (request.data.get("reason") or "").strip()[:255]
            order.cancelled_reason = reason or "أُلغي من الكاونتر"
            fields.append("cancelled_reason")
        elif previous == CANCELLED:
            order.cancelled_reason = ""
            fields.append("cancelled_reason")
        order.save(update_fields=fields)

        # ── leaving `collected`: the sale it made never happened ──────────
        if previous == COLLECTED:
            paid = (order.total or Decimal("0")) - points_service.value_of(spent)
            void_sale_for_order(order)
            points_service.reverse_award(
                order.store, order.customer, paid,
                source="طلب", source_id=order.pk,
                cycle=points_service.cycle_of("reverse", "طلب", order.pk),
            )

        # ── leaving `cancelled`: it is a live order again, so it costs ────
        if previous == CANCELLED and spent:
            points_service.spend_on_purchase(
                order.store, order.customer, spent, order.total,
                source="طلب", source_id=order.pk,
                cycle=points_service.cycle_of("redeem", "طلب", order.pk),
            )

        # ── entering `collected`: takings, stock, points ──────────────────
        if target == COLLECTED:
            sale_for_order(
                order,
                created_by=request.user if request.user.is_authenticated else None,
            )
            paid = (order.total or Decimal("0")) - points_service.value_of(spent)
            earned = points_service.award_for_purchase(
                order.store, order.customer, paid,
                source="طلب", source_id=order.pk,
                cycle=points_service.cycle_of("earn", "طلب", order.pk),
            )
            if earned:
                push_service.notify_points(
                    order.store, order.customer, earned,
                    points_service.balance_of(order.customer),
                    f"من الطلب #{order.pk}",
                )

        # ── entering `cancelled`: give the spent points back ──────────────
        if target == CANCELLED and spent:
            points_service.refund_spend(
                order.store, order.customer, spent,
                source="طلب", source_id=order.pk,
                cycle=points_service.cycle_of("refund", "طلب", order.pk),
            )

        # The customer is waiting on exactly this. Record + push, after commit.
        push_service.notify_order_status(order)
        order.refresh_from_db()
        return Response(self.get_serializer(order).data)


# <scaffold:viewsets>

"""Deep-analysis reports — the paid "reports" module.

Everything here is TENANT-SCOPED: every function takes a store_id that the
views derive server-side from the authenticated user (never from client
input), same as the rest of the app. Endpoints sit behind ModuleEnabled
("reports") + OwnerRequired, so employees and un-subscribed tenants never
reach them.

Reports offered:
- inventory issues: zero-priced, priced below cost, negative stock, missing
  barcode, low stock, dead stock (in stock but unsold for N days)
- inventory valuation: stock value at cost / retail, potential profit
- sales: revenue by day, top / least selling products (by qty or revenue),
  returns counted as negative like every other sales statistic
- xlsx export of any of the above
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import (
    Case,
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    IntegerField,
    Max,
    Min,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Length, TruncDate
from django.utils import timezone

from . import models

# issue key -> Arabic label. Grouped: pricing, stock, barcode, data hygiene.
# broken_barcode is the UMBRELLA barcode filter: empty OR too short (<5) OR
# too long (>15) OR containing non-numeric characters — one card instead of
# four, keeping the KPI grid approachable. The row payload carries WHY.
ISSUES: dict[str, str] = {
    "zero_price": "سعر صفر أو بالسالب",
    "below_cost": "تُباع بأقل من التكلفة",
    "zero_cost": "تكلفة شراء صفر أو بالسالب",
    "negative_stock": "مخزون بالسالب",
    "out_of_stock": "نافذ من المخزون (رصيد صفر)",
    "low_stock": "مخزون منخفض (١ إلى N)",
    "dead_stock": "مخزون راكد (متوفر بلا مبيعات بالفترة)",
    "expired": "منتهي الصلاحية",
    "expiring_soon": "قريب الانتهاء",
    "no_expiry": "بدون تاريخ صلاحية",
    "broken_barcode": "باركود مكسور",
    "duplicate_barcode": "باركود مكرر",
    "no_category": "بدون تصنيف",
    "no_name": "بدون اسم",
    "name_no_letters": "اسم بدون أي حروف",
    "name_length": "اسم قصير جداً أو طويل جداً",
}

#: Default character-length window for a "sensible" product name — the
#: name_length filter flags anything shorter/longer. Owner-tunable per request.
NAME_MIN_DEFAULT = 3
NAME_MAX_DEFAULT = 50

#: barcode "brokenness" predicates — shared by the filter and the counts.
#: Aligned with the SCANNER's accept rule (digits, 4–20 chars — see the
#: frontend's isValidProductBarcode + docs/BARCODE_SCANNING.md): a barcode the
#: scanner happily reads must never be reported as "broken".
_BROKEN_BARCODE_Q = (
    Q(barcode="")
    | Q(barcode__regex=r"^.{1,3}$")
    | Q(barcode__regex=r"^.{21,}$")
    | Q(barcode__regex=r"[^0-9]")
)


def barcode_problem(barcode: str) -> str:
    """Why a barcode counts as broken — mirrored under each row in the UI."""
    if not barcode:
        return "بدون باركود"
    problems = []
    if len(barcode) < 4:
        problems.append("أقصر من ٤ خانات")
    if len(barcode) > 20:
        problems.append("أطول من ٢٠ خانة")
    if not barcode.isdigit():
        problems.append("يحتوي رموزاً غير رقمية")
    return " · ".join(problems)

#: The KPIs surfaced on the summary cards (the rest stay filter-only).
SUMMARY_ISSUES = tuple(ISSUES)

DEC = DecimalField(max_digits=14, decimal_places=2)

#: Matches any Arabic or Latin letter — names failing this are digit/symbol junk.
_LETTERS_RE = r"[A-Za-zء-ي]"


def issue_queryset(
    store_id: int,
    issue: str,
    *,
    low_stock_threshold: Decimal = Decimal("5"),
    dead_days: int = 60,
    include_equal: bool = False,
    name_min: int = NAME_MIN_DEFAULT,
    name_max: int = NAME_MAX_DEFAULT,
):
    """The products matching one inventory issue — tenant-scoped.

    `include_equal` widens below_cost to price <= cost (the "or equal" knob).
    `low_stock_threshold` is the owner-controlled N for low_stock.
    """
    qs = models.Product.objects.for_pharmacy(store_id).select_related(
        "category"
    )
    if issue == "zero_price":
        return qs.filter(price__lte=0)
    if issue == "below_cost":
        base = qs.filter(cost__gt=0, price__gt=0)
        if include_equal:
            return base.filter(price__lte=F("cost"))
        return base.filter(price__lt=F("cost"))
    if issue == "negative_stock":
        return qs.filter(stock__lt=0)
    if issue == "out_of_stock":
        return qs.filter(stock=0)
    if issue == "low_stock":
        # Strictly LOW, not gone: zero belongs to out_of_stock. Keeping the
        # buckets disjoint is what makes the self-checks below possible.
        return qs.filter(stock__gt=0, stock__lte=low_stock_threshold)
    if issue == "dead_stock":
        cutoff = timezone.now() - timedelta(days=dead_days)
        sold_ids = models.SaleItem.objects.for_pharmacy(store_id).filter(
            # sale__created_at (the invoice date), NOT the SaleItem row's
            # created_at — imported history is written now, so its item rows are
            # all "today"; bucketing must follow the real invoice date.
            sale__created_at__gte=cutoff,
            product_id__isnull=False,
        ).values("product_id")
        return qs.filter(stock__gt=0).exclude(id__in=sold_ids)
    if issue in ("expired", "expiring_soon", "no_expiry"):
        return filter_expiry(qs, issue, store_id)
    if issue == "broken_barcode":
        return qs.filter(_BROKEN_BARCODE_Q)
    if issue == "zero_cost":
        return qs.filter(cost__lte=0)
    if issue == "duplicate_barcode":
        dupes = (
            models.Product.objects.for_pharmacy(store_id)
            .exclude(barcode="")
            .values("barcode")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
            .values("barcode")
        )
        return qs.filter(barcode__in=dupes)
    if issue == "no_category":
        return qs.filter(category__isnull=True)
    if issue == "no_name":
        return qs.filter(Q(name="") | Q(name__regex=r"^\s*$"))
    if issue == "name_no_letters":
        # Whitespace-only names belong to no_name, not here.
        return (
            qs.exclude(name="")
            .exclude(name__regex=r"^\s*$")
            .exclude(name__regex=_LETTERS_RE)
        )
    if issue == "name_length":
        # Names shorter than name_min or longer than name_max — junk or a paste
        # error. Empty names belong to no_name, so they're excluded here.
        return (
            qs.exclude(name="")
            .annotate(_nl=Length("name"))
            .filter(Q(_nl__lt=name_min) | Q(_nl__gt=name_max))
        )
    raise ValueError(f"unknown issue: {issue}")


def expiry_default(store_id: int) -> int:
    """The store's near-expiry alert window (days). Falls back to 30."""
    return (
        models.Store.objects.filter(pk=store_id)
        .values_list("expiry_alert_days", flat=True)
        .first()
        or 30
    )


def filter_expiry(qs, status, store_id, today=None):
    """Narrow a Product queryset to expired / expiring-soon (in-stock only).

    Shared by the insights drill-down, the reports list, and the products
    list `?expiry=` filter, so all three agree. Uses the store default window
    (the per-product override drives only the per-row badge in the serializer).
    """
    today = today or timezone.localdate()
    if status == "expired":
        return qs.filter(
            stock__gt=0, expiry_date__isnull=False, expiry_date__lt=today
        )
    if status in ("soon", "expiring_soon"):
        horizon = today + timedelta(days=expiry_default(store_id))
        return qs.filter(
            stock__gt=0, expiry_date__gte=today, expiry_date__lte=horizon
        )
    if status in ("none", "no_expiry"):
        # In-stock items missing an expiry date — a data-quality gap to fill.
        return qs.filter(stock__gt=0, expiry_date__isnull=True)
    return qs


def issue_counts(
    store_id: int,
    *,
    low_stock_threshold: Decimal = Decimal("5"),
    dead_days: int = 60,
    include_equal: bool = False,
) -> dict[str, int]:
    """All KPI counts in as few queries as possible.

    The simple predicates collapse into ONE aggregate pass over the tenant's
    products (conditional counts); only dead_stock and duplicate_barcode
    need their own query. This is the hot path of the summary endpoint —
    at 20k+ listings per store, 13 separate COUNT(*)s add up.
    """
    below = Q(cost__gt=0, price__gt=0) & (
        Q(price__lte=F("cost")) if include_equal else Q(price__lt=F("cost"))
    )
    blank_name = Q(name="") | Q(name__regex=r"^\s*$")
    # Expiry windows are plain date comparisons, so they fold into the single
    # aggregate pass (no extra queries). In-stock only = actionable.
    today = timezone.localdate()
    horizon = today + timedelta(days=expiry_default(store_id))
    agg = models.Product.objects.for_pharmacy(store_id).aggregate(
        zero_price=Count("id", filter=Q(price__lte=0)),
        expired=Count(
            "id",
            filter=Q(stock__gt=0, expiry_date__isnull=False, expiry_date__lt=today),
        ),
        expiring_soon=Count(
            "id",
            filter=Q(stock__gt=0, expiry_date__gte=today, expiry_date__lte=horizon),
        ),
        no_expiry=Count(
            "id", filter=Q(stock__gt=0, expiry_date__isnull=True)
        ),
        below_cost=Count("id", filter=below),
        zero_cost=Count("id", filter=Q(cost__lte=0)),
        negative_stock=Count("id", filter=Q(stock__lt=0)),
        out_of_stock=Count("id", filter=Q(stock=0)),
        low_stock=Count(
            "id", filter=Q(stock__gt=0, stock__lte=low_stock_threshold)
        ),
        broken_barcode=Count("id", filter=_BROKEN_BARCODE_Q),
        no_category=Count("id", filter=Q(category__isnull=True)),
        no_name=Count("id", filter=blank_name),
        name_no_letters=Count(
            "id", filter=~blank_name & ~Q(name__regex=_LETTERS_RE)
        ),
    )
    agg["dead_stock"] = issue_queryset(
        store_id, "dead_stock", dead_days=dead_days
    ).count()
    agg["duplicate_barcode"] = issue_queryset(
        store_id, "duplicate_barcode"
    ).count()
    # Length() doesn't fold into the conditional-count pass — its own cheap query.
    agg["name_length"] = issue_queryset(store_id, "name_length").count()
    return {key: agg[key] for key in ISSUES}


def category_breakdown(store_id: int, limit: int = 12, base_qs=None) -> list[dict]:
    """Per-category inventory snapshot for the pie chart + rich tooltip.

    One aggregate query: count, priced min/max, in-stock count, stock value
    at retail. Top `limit` categories by size; uncategorised shows as بلا.
    `base_qs` narrows it to a filtered subset (charts track the active filter).
    """
    rows = (
        (base_qs if base_qs is not None else models.Product.objects.for_pharmacy(store_id))
        .values("category__name")
        .annotate(
            count=Count("id"),
            in_stock=Count("id", filter=Q(stock__gt=0)),
            cheapest=Min("price", filter=Q(price__gt=0)),
            priciest=Max("price"),
            stock_value=Coalesce(
                Sum(
                    ExpressionWrapper(F("stock") * F("price"), output_field=DEC),
                    filter=Q(stock__gt=0),
                ),
                Value(Decimal("0"), output_field=DEC),
            ),
        )
        .order_by("-count")[:limit]
    )
    return [
        {
            "name": r["category__name"] or "بلا تصنيف",
            "count": r["count"],
            "in_stock": r["in_stock"],
            "cheapest": str(r["cheapest"] or Decimal("0")),
            "priciest": str(r["priciest"] or Decimal("0")),
            "stock_value": str(r["stock_value"]),
        }
        for r in rows
    ]


def teaser(store_id: int) -> dict:
    """The REAL-but-limited numbers for the upsell teaser.

    Deliberately available to any authenticated staff member of the store
    (no reports module, no owner role): a handful of counts and the top
    seller's NAME — enough to intrigue, nothing detailed. Cheap: one
    aggregate + one small ranking query.
    """
    agg = models.Product.objects.for_pharmacy(store_id).aggregate(
        zero_price=Count("id", filter=Q(price__lte=0)),
        below_cost=Count("id", filter=Q(cost__gt=0, price__gt=0, price__lt=F("cost"))),
        negative_stock=Count("id", filter=Q(stock__lt=0)),
    )
    top = product_sales(store_id, days=30, direction="top", limit=1)
    return {
        "zero_price": agg["zero_price"],
        "below_cost": agg["below_cost"],
        "negative_stock": agg["negative_stock"],
        "top_product": top[0]["name"] if top else "",
    }


def inventory_valuation(store_id: int, base_qs=None) -> dict:
    """Stock value at cost & retail (positive stock only) + catalogue size.

    `base_qs` narrows the numbers to a filtered subset (so the charts can track
    the active filter); default = the whole tenant catalogue.
    """
    meds = base_qs if base_qs is not None else (
        models.Product.objects.for_pharmacy(store_id)
    )
    positive = meds.filter(stock__gt=0)
    agg = positive.aggregate(
        cost_value=Coalesce(
            Sum(ExpressionWrapper(F("stock") * F("cost"), output_field=DEC)),
            Value(Decimal("0"), output_field=DEC),
        ),
        retail_value=Coalesce(
            Sum(ExpressionWrapper(F("stock") * F("price"), output_field=DEC)),
            Value(Decimal("0"), output_field=DEC),
        ),
    )
    return {
        "total_medications": meds.count(),
        "in_stock": positive.count(),
        "stock_cost_value": str(agg["cost_value"]),
        "stock_retail_value": str(agg["retail_value"]),
        "potential_profit": str(agg["retail_value"] - agg["cost_value"]),
    }


def build_filtered_queryset(
    store_id: int,
    *,
    issue: str = "all",
    search: str = "",
    category: str = "",
    manufacturer: str = "",
    price_min=None,
    price_max=None,
    stock_min=None,
    stock_max=None,
    low_stock_threshold: Decimal = Decimal("5"),
    dead_days: int = 60,
    include_equal: bool = False,
    name_min: int = NAME_MIN_DEFAULT,
    name_max: int = NAME_MAX_DEFAULT,
):
    """The exact queryset behind the products table AND the filtered charts, so
    the two always agree. `issue='all'` runs the advanced ranges over the whole
    catalogue; any known issue narrows first, then search/ranges apply."""
    if issue == "all":
        qs = models.Product.objects.for_pharmacy(store_id).select_related(
            "category"
        )
    else:
        qs = issue_queryset(
            store_id,
            issue,
            low_stock_threshold=low_stock_threshold,
            dead_days=dead_days,
            include_equal=include_equal,
            name_min=name_min,
            name_max=name_max,
        )
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(barcode__istartswith=search)
            | Q(alt_barcodes__icontains=f'"{search}')
        )
    if category:
        qs = qs.filter(category__name__icontains=category)
    if manufacturer:
        qs = qs.filter(manufacturer__name__icontains=manufacturer)
    if price_min is not None:
        qs = qs.filter(price__gte=price_min)
    if price_max is not None:
        qs = qs.filter(price__lte=price_max)
    if stock_min is not None:
        qs = qs.filter(stock__gte=stock_min)
    if stock_max is not None:
        qs = qs.filter(stock__lte=stock_max)
    return qs


def filtered_charts(store_id: int, base_qs) -> dict:
    """Valuation + category breakdown over a pre-filtered queryset — the two
    reports-page charts that track the active filter."""
    return {
        "valuation": inventory_valuation(store_id, base_qs),
        "categories": category_breakdown(store_id, base_qs=base_qs),
    }


def _signed(expr):
    """Returns count as negative for returns — 'استرجاع' lowers every stat."""
    sign = Case(
        When(sale__is_return=True, then=Value(-1)),
        default=Value(1),
        output_field=IntegerField(),
    )
    return ExpressionWrapper(expr * sign, output_field=DEC)


def product_sales(
    store_id: int,
    *,
    days: int = 30,
    by: str = "qty",
    direction: str = "top",
    limit: int = 10,
    category: str = "",
):
    """Best / worst selling products (among products that DID sell).

    Grouped by product + snapshot name so analytics survive catalogue
    edits; variants roll up into their product. `by` = qty | revenue.
    """
    since = timezone.now() - timedelta(days=max(1, min(days, 365)))
    items = models.SaleItem.objects.for_pharmacy(store_id).filter(
        sale__created_at__gte=since  # real invoice date (imports bucket right)
    )
    if category:
        items = items.filter(category=category)
    order_field = "qty" if by != "revenue" else "revenue"
    ordering = f"-{order_field}" if direction != "bottom" else order_field
    rows = (
        items.values("product_id", "medication_name")
        .annotate(
            qty=Coalesce(Sum(_signed(F("quantity"))), Value(Decimal("0"), output_field=DEC)),
            revenue=Coalesce(
                Sum(_signed(F("quantity") * F("unit_price"))),
                Value(Decimal("0"), output_field=DEC),
            ),
            sales=Count("sale_id", distinct=True),
        )
        .order_by(ordering, "medication_name")[: max(1, min(limit, 100))]
    )
    return [
        {
            "product_id": r["product_id"],
            "name": r["medication_name"] or "—",
            "quantity": str(r["qty"]),
            "revenue": str(r["revenue"]),
            "sales": r["sales"],
        }
        for r in rows
    ]


def restock_quota(
    store_id: int,
    *,
    days: int = 30,
    cover_days: int = 30,
    low_stock_threshold: Decimal = Decimal("5"),
):
    """Suggested purchase list for the purchases page.

    A product is a candidate when its stock is at or below its reorder level (or
    the global low-stock threshold when no reorder level is set). Suggested qty
    covers `cover_days` at the product's recent daily velocity, never below its
    reorder level. Only actionable rows (suggested_qty > 0) are returned. Money
    and quantities come back as strings, like every other reports payload.
    """
    from decimal import ROUND_CEILING

    days = max(1, min(days, 365))
    cover = Decimal(max(1, min(cover_days, 365)))
    since = timezone.now() - timedelta(days=days)

    # Net units sold per product over the window (returns subtracted). Filter on
    # the PARENT sale's date so imported history — whose SaleItem.created_at is
    # the import time — buckets by the real invoice date.
    sold = {
        r["product_id"]: r["qty"]
        for r in (
            models.SaleItem.objects.for_pharmacy(store_id)
            .filter(sale__created_at__gte=since)
            .values("product_id")
            .annotate(
                qty=Coalesce(
                    Sum(_signed(F("quantity"))), Value(Decimal("0"), output_field=DEC)
                )
            )
        )
        if r["product_id"] is not None
    }

    candidates = (
        models.Product.objects.for_pharmacy(store_id)
        .select_related("category", "manufacturer")
        .filter(
            Q(reorder_level__gt=0, stock__lte=F("reorder_level"))
            | Q(reorder_level=Decimal("0"), stock__lte=low_stock_threshold)
        )
    )

    dwin = Decimal(days)
    rows = []
    total_cost = Decimal("0.00")
    total_gain = Decimal("0.00")
    for m in candidates.iterator():
        units_sold = sold.get(m.id, Decimal("0"))
        want = (units_sold / dwin) * cover  # units to cover the window at velocity
        suggest = max(
            want - m.stock,
            (m.reorder_level or Decimal("0")) - m.stock,
            Decimal("0"),
        ).to_integral_value(rounding=ROUND_CEILING)
        if suggest <= 0:
            continue
        buy_cost = (suggest * m.cost).quantize(Decimal("0.01"))
        gain = (suggest * (m.price - m.cost)).quantize(Decimal("0.01"))
        total_cost += buy_cost
        total_gain += gain
        rows.append(
            {
                "product_id": m.id,
                "name": m.name,
                "barcode": m.barcode,
                "category": m.category.name if m.category_id else "",
                "manufacturer": m.manufacturer.name if m.manufacturer_id else "",
                "stock": str(m.stock),
                "reorder_level": str(m.reorder_level),
                "cost": str(m.cost),
                "price": str(m.price),
                "sold": str(units_sold),
                "suggested_qty": str(suggest),
                "buy_cost": str(buy_cost),
                "projected_gain": str(gain),
            }
        )
    rows.sort(key=lambda r: Decimal(r["buy_cost"]), reverse=True)
    return {
        "days": days,
        "cover_days": int(cover),
        "low_stock_threshold": str(low_stock_threshold),
        "count": len(rows),
        "total_buy_cost": str(total_cost),
        "total_projected_gain": str(total_gain),
        "results": rows,
    }


def scans(store_id: int, *, days: int = 30, limit: int = 5000) -> dict:
    """Customer price-check scan analytics — the Reports "تقارير المسح" section.

    Folds the daily ``ScanDaily`` counters over the window into one payload the
    frontend renders whole (then filters / searches / sorts / charts in the
    browser):

      • ``summary``   — total scans, matched vs not-found (both as scans AND as
                        distinct barcodes), and the match rate.
      • ``by_day``    — scans per day, split matched / not-found (the trend).
      • ``products``  — one row per barcode: how many times scanned, on how many
                        days, last seen, whether it matches a product, and — for
                        matches — the CURRENT price / cost / stock so the owner
                        can reprice or reorder straight from the report.

    Anonymous throughout: only barcodes, counts, and (for matches) the product.
    """
    days = max(1, min(days, 365))
    today = timezone.localdate()
    since = today - timedelta(days=days - 1)

    base = models.ScanDaily.objects.for_pharmacy(store_id).filter(day__gte=since)

    matched_units = Case(
        When(found=True, then=F("count")),
        default=Value(0),
        output_field=IntegerField(),
    )

    # Per-day trend (matched vs not-found) for the over-time chart.
    day_rows = (
        base.values("day")
        .annotate(
            total=Coalesce(Sum("count"), Value(0)),
            matched=Coalesce(Sum(matched_units), Value(0)),
        )
        .order_by("day")
    )
    by_day = [
        {
            "day": r["day"].isoformat(),
            "total": r["total"],
            "matched": r["matched"],
            "not_found": r["total"] - r["matched"],
        }
        for r in day_rows
    ]

    # Per-barcode aggregate over the whole window.
    per = list(
        base.values("barcode")
        .annotate(
            count=Coalesce(Sum("count"), Value(0)),
            days=Count("day", distinct=True),
            last_day=Max("day"),
            ever_found=Max(
                Case(
                    When(found=True, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            ),
            med_id=Max("product_id"),
            snap_name=Max("medication_name"),
        )
        .order_by("-count", "barcode")[: max(1, min(limit, 20000))]
    )

    # Enrich matched rows with CURRENT price / cost / stock (one query) so the
    # owner can act — reprice or reorder — right from the report.
    med_ids = [r["med_id"] for r in per if r["med_id"]]
    live = {}
    if med_ids:
        for m in (
            models.Product.objects.for_pharmacy(store_id)
            .filter(id__in=med_ids)
            .values("id", "name", "price", "cost", "stock", "barcode")
        ):
            live[m["id"]] = m

    products = []
    total_scans = matched_scans = matched_barcodes = 0
    for r in per:
        found = bool(r["ever_found"])
        cnt = int(r["count"] or 0)
        total_scans += cnt
        if found:
            matched_scans += cnt
            matched_barcodes += 1
        m = live.get(r["med_id"]) if r["med_id"] else None
        products.append(
            {
                "barcode": r["barcode"],
                "name": (m["name"] if m else "") or r["snap_name"] or "",
                "product_id": r["med_id"] if m else None,
                "found": found,
                "count": cnt,
                "days": r["days"],
                "last_day": r["last_day"].isoformat() if r["last_day"] else None,
                "price": str(m["price"]) if m else None,
                "cost": str(m["cost"]) if m else None,
                "stock": str(m["stock"]) if m else None,
            }
        )

    distinct_barcodes = len(per)
    not_found_scans = total_scans - matched_scans
    rate = (
        (Decimal(matched_scans) / Decimal(total_scans)).quantize(Decimal("0.01"))
        if total_scans
        else Decimal("0.00")
    )
    return {
        "days": days,
        "from": since.isoformat(),
        "to": today.isoformat(),
        "summary": {
            "total_scans": total_scans,
            "matched_scans": matched_scans,
            "not_found_scans": not_found_scans,
            "distinct_barcodes": distinct_barcodes,
            "matched_barcodes": matched_barcodes,
            "not_found_barcodes": distinct_barcodes - matched_barcodes,
            "matched_rate": str(rate),
        },
        "by_day": by_day,
        "products": products,
    }


def sales_by_day(store_id: int, *, days: int = 30) -> list[dict]:
    since = timezone.now() - timedelta(days=max(1, min(days, 365)))
    sign = Case(
        When(is_return=True, then=Value(-1)),
        default=Value(1),
        output_field=IntegerField(),
    )
    rows = (
        models.Sale.objects.for_pharmacy(store_id)
        .filter(created_at__gte=since)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            total=Coalesce(
                Sum(ExpressionWrapper(F("discounted_total") * sign, output_field=DEC)),
                Value(Decimal("0"), output_field=DEC),
            ),
            count=Count("id"),
        )
        .order_by("day")
    )
    return [
        {"day": r["day"].isoformat(), "total": str(r["total"]), "count": r["count"]}
        for r in rows
    ]


def sales_summary(store_id: int, *, days: int = 30) -> dict:
    """Deep sales analytics — its own paid module ("sales_reports").

    Everything in one response so the frontend renders a full tab from a
    single (cached) call: totals, average basket, cash/debt split, returns,
    revenue by day, by hour-of-day, by employee, by category, top customers.
    Returns count as negative throughout, like every other sales statistic.
    """
    from django.db.models.functions import ExtractHour

    since = timezone.now() - timedelta(days=max(1, min(days, 365)))
    sign = Case(
        When(is_return=True, then=Value(-1)),
        default=Value(1),
        output_field=IntegerField(),
    )
    signed_total = ExpressionWrapper(F("discounted_total") * sign, output_field=DEC)
    zero = Value(Decimal("0"), output_field=DEC)

    sales = models.Sale.objects.for_pharmacy(store_id).filter(
        created_at__gte=since
    )
    agg = sales.aggregate(
        revenue=Coalesce(Sum(signed_total), zero),
        count=Count("id"),
        returns_count=Count("id", filter=Q(is_return=True)),
        returns_value=Coalesce(
            Sum("discounted_total", filter=Q(is_return=True)), zero
        ),
        cash=Coalesce(Sum(signed_total, filter=Q(payment_method="cash")), zero),
        debt=Coalesce(Sum(signed_total, filter=Q(payment_method="debt")), zero),
    )
    forward_count = max(agg["count"] - agg["returns_count"], 0)
    avg_basket = (agg["revenue"] / forward_count) if forward_count else Decimal("0")

    by_hour_rows = (
        sales.annotate(h=ExtractHour("created_at"))
        .values("h")
        .annotate(total=Coalesce(Sum(signed_total), zero), count=Count("id"))
        .order_by("h")
    )
    by_employee = [
        {
            "name": r["created_by__display_name"] or r["created_by__username"] or "—",
            "total": str(r["total"]),
            "count": r["count"],
        }
        for r in (
            sales.values("created_by__username", "created_by__display_name")
            .annotate(total=Coalesce(Sum(signed_total), zero), count=Count("id"))
            .order_by("-total")[:10]
        )
    ]
    by_category = [
        {"name": r["category"] or "بلا تصنيف", "revenue": str(r["revenue"]), "qty": str(r["qty"])}
        for r in (
            models.SaleItem.objects.for_pharmacy(store_id)
            .filter(sale__created_at__gte=since)  # real invoice date
            .values("category")
            .annotate(
                revenue=Coalesce(Sum(_signed(F("quantity") * F("unit_price"))), zero),
                qty=Coalesce(Sum(_signed(F("quantity"))), zero),
            )
            .order_by("-revenue")[:10]
        )
    ]
    top_customers = [
        {"name": r["customer__name"], "total": str(r["total"]), "count": r["count"]}
        for r in (
            sales.filter(customer__isnull=False)
            .values("customer__name")
            .annotate(total=Coalesce(Sum(signed_total), zero), count=Count("id"))
            .order_by("-total")[:10]
        )
    ]

    return {
        "days": days,
        "revenue": str(agg["revenue"]),
        "count": agg["count"],
        "avg_basket": str(avg_basket.quantize(Decimal("0.01"))),
        "returns": {"count": agg["returns_count"], "value": str(agg["returns_value"])},
        "payment_split": {"cash": str(agg["cash"]), "debt": str(agg["debt"])},
        "by_day": sales_by_day(store_id, days=days),
        "by_hour": [
            {"hour": r["h"], "total": str(r["total"]), "count": r["count"]}
            for r in by_hour_rows
        ],
        "by_employee": by_employee,
        "by_category": by_category,
        "top_customers": top_customers,
        "top_products": product_sales(store_id, days=days, direction="top", limit=5),
        "least_products": product_sales(
            store_id, days=days, direction="bottom", limit=5
        ),
    }


def consistency_checks(
    store_id: int,
    issues: dict[str, int],
    valuation: dict,
    *,
    dead_days: int,
) -> dict:
    """Machine-verified invariants: the SAME numbers computed two independent
    ways must agree. If any check fails, the payload says so loudly instead of
    silently showing wrong stats — the UI surfaces it and we get a bug report
    with the exact failing identity.

    Invariants:
      1. متوفر + نافذ + بالسالب == كل الأصناف   (stock partitions the catalogue)
      2. نافذ == count(stock = 0)                (recount, independent path)
      3. راكد == متوفر − أصناف متوفرة بِيعت بالفترة (the user's own logic!)
      4. سعر صفر أو بالسالب == الكل − count(price > 0)
      5. منخفض ⊆ متوفر  (low_stock can never exceed in-stock)
    """
    meds = models.Product.objects.for_pharmacy(store_id)
    agg = meds.aggregate(
        zero_stock=Count("id", filter=Q(stock=0)),
        neg_stock=Count("id", filter=Q(stock__lt=0)),
        priced=Count("id", filter=Q(price__gt=0)),
    )
    total = valuation["total_medications"]
    in_stock = valuation["in_stock"]
    cutoff = timezone.now() - timedelta(days=dead_days)
    stocked_sold = (
        meds.filter(stock__gt=0)
        .filter(
            id__in=models.SaleItem.objects.for_pharmacy(store_id).filter(
                sale__created_at__gte=cutoff,  # real invoice date (matches dead_stock)
                product_id__isnull=False,
            ).values("product_id")
        )
        .count()
    )
    details = []

    def check(name: str, label: str, actual: int, expected: int) -> None:
        details.append(
            {
                "name": name,
                "label": label,
                "actual": actual,
                "expected": expected,
                "ok": actual == expected,
            }
        )

    check(
        "stock_partition",
        "متوفر + نافذ + بالسالب = كل الأصناف",
        in_stock + agg["zero_stock"] + agg["neg_stock"],
        total,
    )
    check("out_of_stock", "نافذ = عدّ مستقل للرصيد صفر", issues["out_of_stock"], agg["zero_stock"])
    check("negative_stock", "بالسالب = عدّ مستقل", issues["negative_stock"], agg["neg_stock"])
    check(
        "dead_stock",
        "راكد = المتوفر − المتوفر الذي بِيع خلال الفترة",
        issues["dead_stock"],
        in_stock - stocked_sold,
    )
    check("zero_price", "سعر ≤ صفر = الكل − المسعّر", issues["zero_price"], total - agg["priced"])
    details.append(
        {
            "name": "low_stock_bounded",
            "label": "المنخفض لا يتجاوز المتوفر",
            "actual": issues["low_stock"],
            "expected": in_stock,
            "ok": issues["low_stock"] <= in_stock,
        }
    )
    return {"passed": all(d["ok"] for d in details), "details": details}


def summary(store_id: int, *, days: int = 30) -> dict:
    """The reports-page overview: issues + valuation + categories + sales.

    The period selector governs the sales block AND the dead-stock horizon —
    «راكد» always answers "لم يُبَع خلال الفترة المختارة", matching what the
    period chips promise.
    """
    by_day = sales_by_day(store_id, days=days)
    total = sum(Decimal(d["total"]) for d in by_day) if by_day else Decimal("0")
    issues = issue_counts(store_id, dead_days=days)
    valuation = inventory_valuation(store_id)
    return {
        "meta": {
            "days": days,
            "dead_days": days,
            "low_stock_threshold": 5,
            "generated_at": timezone.now().isoformat(),
        },
        "issues": issues,
        "checks": consistency_checks(store_id, issues, valuation, dead_days=days),
        "valuation": valuation,
        "categories": category_breakdown(store_id),
        "sales": {
            "days": days,
            "revenue": str(total),
            "count": sum(d["count"] for d in by_day),
            "by_day": by_day,
            "top_products": product_sales(store_id, days=days, direction="top", limit=5),
            "least_products": product_sales(
                store_id, days=days, direction="bottom", limit=5
            ),
        },
    }


# --------------------------------------------------------------------------
# xlsx export
# --------------------------------------------------------------------------

#: Characters Excel forbids in a worksheet title.
_INVALID_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def _sheet_title(label: str, used: set[str]) -> str:
    """A valid, unique Excel worksheet title: ≤31 chars, no []:*?/\\, no dupes."""
    base = _INVALID_SHEET_CHARS.sub(" ", label).strip()[:31] or "ورقة"
    title, n = base, 2
    while title.casefold() in used:
        suffix = f" {n}"
        title = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(title.casefold())
    return title


def _issue_rows(qs):
    yield ["الاسم", "الباركود", "التصنيف", "السعر", "التكلفة", "المخزون"]
    for m in qs.values(
        "name", "barcode", "category__name", "price", "cost", "stock"
    ).iterator():
        yield [
            m["name"],
            m["barcode"],
            m["category__name"] or "",
            float(m["price"]),
            float(m["cost"]),
            float(m["stock"]),
        ]


def build_export_workbook(store, *, report: str, **opts):
    """An openpyxl Workbook for one report. Caller streams it as a download."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active

    if report == "issues":
        issue = opts.get("issue") or "zero_price"
        if issue not in ISSUES:
            raise ValueError("unknown issue")
        ws.title = "تقرير"[:31]
        ws.append([f"{ISSUES[issue]} — {store.name}"])
        for row in _issue_rows(
            issue_queryset(
                store.id,
                issue,
                low_stock_threshold=opts.get("low_stock_threshold", Decimal("5")),
                dead_days=opts.get("dead_days", 60),
                include_equal=opts.get("include_equal", False),
            )
        ):
            ws.append(row)
    elif report == "all_issues":
        # One worksheet per inventory issue — each sheet lists ALL products
        # matching that filter. First sheet reuses the default; the rest are
        # created in ISSUES order so the workbook reads top-to-bottom.
        used: set[str] = set()
        for i, (issue, label) in enumerate(ISSUES.items()):
            sheet = ws if i == 0 else wb.create_sheet()
            sheet.title = _sheet_title(label, used)
            sheet.append([f"{label} — {store.name}"])
            for row in _issue_rows(
                issue_queryset(
                    store.id,
                    issue,
                    low_stock_threshold=opts.get(
                        "low_stock_threshold", Decimal("5")
                    ),
                    dead_days=opts.get("dead_days", 60),
                    include_equal=opts.get("include_equal", False),
                )
            ):
                sheet.append(row)
    elif report == "top_products":
        days = opts.get("days", 30)
        ws.title = "الأكثر مبيعاً"[:31]
        ws.append([f"المنتجات الأكثر/الأقل مبيعاً خلال {days} يوماً — {store.name}"])
        ws.append(["الاسم", "الكمية", "الإيراد", "عدد الفواتير"])
        for direction in ("top", "bottom"):
            rows = product_sales(
                store.id,
                days=days,
                by=opts.get("by", "qty"),
                direction=direction,
                limit=opts.get("limit", 20),
            )
            ws.append([("الأكثر مبيعاً" if direction == "top" else "الأقل مبيعاً")])
            for r in rows:
                ws.append([r["name"], float(r["quantity"]), float(r["revenue"]), r["sales"]])
    elif report == "sales":
        data = sales_summary(store.id, days=opts.get("days", 30))
        ws.title = "المبيعات"[:31]
        ws.append([f"تقرير المبيعات — آخر {data['days']} يوماً — {store.name}"])
        ws.append(["الإيراد", data["revenue"]])
        ws.append(["عدد العمليات", data["count"]])
        ws.append(["متوسط الفاتورة", data["avg_basket"]])
        ws.append(["مرتجعات", data["returns"]["count"], data["returns"]["value"]])
        ws.append(["نقدي", data["payment_split"]["cash"]])
        ws.append(["دين", data["payment_split"]["debt"]])
        for section, rows, headers in (
            ("حسب الموظف", data["by_employee"], ["الموظف", "الإيراد", "العمليات"]),
            ("حسب التصنيف", data["by_category"], ["التصنيف", "الإيراد", "الكمية"]),
            ("أفضل الزبائن", data["top_customers"], ["الزبون", "الإيراد", "العمليات"]),
        ):
            ws.append([])
            ws.append([section])
            ws.append(headers)
            for r in rows:
                vals = list(r.values())
                ws.append([vals[0], float(vals[1]), float(vals[2])])
    elif report == "summary":
        data = summary(store.id, days=opts.get("days", 30))
        ws.title = "ملخص"[:31]
        ws.append([f"ملخص التقارير — {store.name}"])
        ws.append([])
        ws.append(["مؤشرات المخزون"])
        for key, label in ISSUES.items():
            ws.append([label, data["issues"][key]])
        ws.append([])
        ws.append(["تقييم المخزون"])
        for k, label in [
            ("total_medications", "عدد الأصناف"),
            ("in_stock", "أصناف متوفرة"),
            ("stock_cost_value", "قيمة المخزون (تكلفة)"),
            ("stock_retail_value", "قيمة المخزون (بيع)"),
            ("potential_profit", "الربح المتوقع"),
        ]:
            ws.append([label, str(data["valuation"][k])])
        ws.append([])
        ws.append(["مبيعات آخر %d يوماً" % data["sales"]["days"]])
        ws.append(["الإيراد", str(data["sales"]["revenue"])])
        ws.append(["عدد الفواتير", data["sales"]["count"]])
    else:
        raise ValueError("unknown report")

    return wb


# ═══════════════════════════════════════════════════════════════════════════
# The café report
# ═══════════════════════════════════════════════════════════════════════════
#
# Everything above this line was written for a shop that holds stock: what is
# priced wrong, what is expiring, what has no barcode, what is worth how much
# on the shelf. A café holds almost no stock. It holds a MENU, and the
# questions it has are about drinks and people:
#
#   what sells, what does not, and what makes the money (not the same list);
#   when is it busy, so who is rostered when;
#   which sizes people actually buy;
#   how much of the till is the app, and how much of it is regulars;
#   what the loyalty scheme costs and what it brings back.
#
# One response, one cache entry, because the page is one screen.


def _cafe_window(days: int):
    days = max(1, min(int(days or 30), 365))
    return days, timezone.now() - timedelta(days=days)


def cafe_summary(store_id: int, *, days: int = 30) -> dict:
    """Everything the coffee-shop reports page shows, in one query set."""
    from django.db.models.functions import ExtractHour, ExtractWeekDay

    days, since = _cafe_window(days)
    zero = Value(Decimal("0"), output_field=DEC)
    sign = Case(
        When(is_return=True, then=Value(-1)),
        default=Value(1),
        output_field=IntegerField(),
    )
    signed_total = ExpressionWrapper(F("discounted_total") * sign, output_field=DEC)

    sales = models.Sale.objects.for_pharmacy(store_id).filter(created_at__gte=since)
    items = models.SaleItem.objects.for_pharmacy(store_id).filter(
        sale__created_at__gte=since
    )

    # ── the headline ─────────────────────────────────────────────────────
    head = sales.aggregate(
        revenue=Coalesce(Sum(signed_total), zero),
        tickets=Count("id", filter=Q(is_return=False)),
        returns=Count("id", filter=Q(is_return=True)),
        with_customer=Count("id", filter=Q(is_return=False, customer__isnull=False)),
    )
    cups = items.aggregate(n=Coalesce(Sum(_signed(F("quantity"))), zero))["n"]
    tickets = head["tickets"] or 0
    avg_ticket = (head["revenue"] / tickets) if tickets else Decimal("0")
    cups_per_ticket = (cups / tickets) if tickets else Decimal("0")

    # ── drinks ───────────────────────────────────────────────────────────
    # Two lists, deliberately. The drink you sell most of and the drink that
    # earns most are usually different, and a café that only ever looks at
    # the first one keeps promoting its cheapest cup.
    top_by_cups = product_sales(store_id, days=days, by="qty", limit=10)
    top_by_revenue = product_sales(store_id, days=days, by="revenue", limit=10)
    slowest = product_sales(store_id, days=days, by="qty", direction="bottom", limit=10)

    # On the menu, and sold NOTHING in the window. product_sales can only
    # rank what appears in a sale, so a drink nobody ordered is invisible to
    # it — which is exactly the drink worth knowing about.
    sold_ids = set(
        items.exclude(product_id=None).values_list("product_id", flat=True).distinct()
    )
    never_sold = [
        {"product_id": p["id"], "name": p["name"], "price": str(p["price"] or 0)}
        for p in (
            models.Product.objects.for_pharmacy(store_id)
            .filter(is_active=True)
            .exclude(id__in=sold_ids)
            .order_by("name")
            .values("id", "name", "price")[:50]
        )
    ]

    # Which SIZE people buy. `variant_label` is snapshotted on the line, so
    # this survives a menu edit.
    by_size = [
        {
            "label": r["variant_label"] or "بلا حجم",
            "qty": str(r["qty"]),
            "revenue": str(r["revenue"]),
        }
        for r in (
            items.exclude(variant_label="")
            .values("variant_label")
            .annotate(
                qty=Coalesce(Sum(_signed(F("quantity"))), zero),
                revenue=Coalesce(
                    Sum(_signed(F("quantity") * F("unit_price"))), zero
                ),
            )
            .order_by("-qty")[:10]
        )
    ]

    by_category = [
        {
            "name": r["category"] or "بلا تصنيف",
            "qty": str(r["qty"]),
            "revenue": str(r["revenue"]),
        }
        for r in (
            items.values("category")
            .annotate(
                qty=Coalesce(Sum(_signed(F("quantity"))), zero),
                revenue=Coalesce(
                    Sum(_signed(F("quantity") * F("unit_price"))), zero
                ),
            )
            .order_by("-revenue")[:12]
        )
    ]

    # ── when ─────────────────────────────────────────────────────────────
    by_hour = [
        {"hour": r["h"], "revenue": str(r["total"]), "count": r["count"]}
        for r in (
            sales.annotate(h=ExtractHour("created_at"))
            .values("h")
            .annotate(total=Coalesce(Sum(signed_total), zero), count=Count("id"))
            .order_by("h")
        )
    ]
    # Django's ExtractWeekDay is 1=Sunday … 7=Saturday, which is already the
    # week as it is read here.
    WEEK = ["الأحد", "الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"]
    by_weekday = [
        {
            "day": WEEK[(r["w"] - 1) % 7],
            "index": r["w"],
            "revenue": str(r["total"]),
            "count": r["count"],
        }
        for r in (
            sales.annotate(w=ExtractWeekDay("created_at"))
            .values("w")
            .annotate(total=Coalesce(Sum(signed_total), zero), count=Count("id"))
            .order_by("w")
        )
    ]
    peak_hour = max(by_hour, key=lambda r: int(r["count"]), default=None)
    peak_day = max(by_weekday, key=lambda r: int(r["count"]), default=None)

    # ── the app vs the counter ───────────────────────────────────────────
    orders = models.Order.objects.for_pharmacy(store_id).filter(created_at__gte=since)
    order_agg = orders.aggregate(
        total=Count("id"),
        collected=Count("id", filter=Q(status=models.Order.Status.COLLECTED)),
        cancelled=Count("id", filter=Q(status=models.Order.Status.CANCELLED)),
        open=Count(
            "id",
            filter=Q(
                status__in=[
                    models.Order.Status.PLACED,
                    models.Order.Status.ACCEPTED,
                    models.Order.Status.PREPARING,
                    models.Order.Status.READY,
                ]
            ),
        ),
    )
    app_revenue = orders.filter(
        status=models.Order.Status.COLLECTED
    ).aggregate(v=Coalesce(Sum("total"), zero))["v"]

    # How long between "I ordered" and "here you go". The Sale is created at
    # the moment of handover, so the gap between the two rows IS the wait —
    # no extra timestamps needed on Order.
    waits = [
        (o.sale.created_at - o.created_at).total_seconds() / 60
        for o in orders.filter(
            status=models.Order.Status.COLLECTED, sale__isnull=False
        ).select_related("sale")[:500]
        if o.sale and o.sale.created_at > o.created_at
    ]
    waits.sort()
    median_wait = waits[len(waits) // 2] if waits else None

    # ── loyalty ──────────────────────────────────────────────────────────
    ledger = models.BeanLedger.objects.for_pharmacy(store_id).filter(
        created_at__gte=since
    )
    points = ledger.aggregate(
        earned=Coalesce(Sum("delta", filter=Q(delta__gt=0)), Value(0)),
        spent=Coalesce(Sum("delta", filter=Q(delta__lt=0)), Value(0)),
        redemptions=Count(
            "id", filter=Q(reason=models.BeanLedger.Reason.REDEEM)
        ),
    )
    per_ils = int(getattr(settings, "POINTS_PER_ILS", 10)) or 10
    earned = int(points["earned"] or 0)
    spent = -int(points["spent"] or 0)

    customers = models.Customer.objects.for_pharmacy(store_id)
    new_customers = customers.filter(created_at__gte=since).count()
    # A "regular" is somebody who came more than once IN THIS WINDOW. Anything
    # cleverer needs a definition the shop has not given us.
    visit_counts = (
        sales.filter(is_return=False, customer__isnull=False)
        .values("customer_id")
        .annotate(n=Count("id"))
    )
    seen = list(visit_counts)
    repeat = sum(1 for r in seen if r["n"] > 1)
    identified = len(seen)

    top_customers = [
        {
            "id": r["customer_id"],
            "name": r["customer__name"] or "—",
            "total": str(r["total"]),
            "visits": r["visits"],
        }
        for r in (
            sales.filter(customer__isnull=False)
            .values("customer_id", "customer__name")
            .annotate(
                total=Coalesce(Sum(signed_total), zero),
                visits=Count("id", filter=Q(is_return=False)),
            )
            .order_by("-total")[:10]
        )
    ]

    return {
        "days": days,
        "headline": {
            "revenue": str(head["revenue"]),
            "tickets": tickets,
            "returns": head["returns"],
            "cups": str(cups),
            "avg_ticket": str(avg_ticket.quantize(Decimal("0.01"))),
            "cups_per_ticket": str(cups_per_ticket.quantize(Decimal("0.01"))),
            "identified_share": (
                round(100 * (head["with_customer"] or 0) / tickets) if tickets else 0
            ),
            "peak_hour": peak_hour["hour"] if peak_hour else None,
            "peak_day": peak_day["day"] if peak_day else None,
        },
        "drinks": {
            "top_by_cups": top_by_cups,
            "top_by_revenue": top_by_revenue,
            "slowest": slowest,
            "never_sold": never_sold,
            "by_size": by_size,
            "by_category": by_category,
        },
        "when": {
            "by_day": sales_by_day(store_id, days=days),
            "by_hour": by_hour,
            "by_weekday": by_weekday,
        },
        "app": {
            "orders": order_agg["total"],
            "collected": order_agg["collected"],
            "cancelled": order_agg["cancelled"],
            "open": order_agg["open"],
            "cancel_rate": (
                round(100 * order_agg["cancelled"] / order_agg["total"])
                if order_agg["total"]
                else 0
            ),
            "revenue": str(app_revenue),
            "share": (
                round(100 * float(app_revenue) / float(head["revenue"]))
                if head["revenue"]
                else 0
            ),
            "median_wait_min": round(median_wait, 1) if median_wait is not None else None,
        },
        "loyalty": {
            "earned": earned,
            "spent": spent,
            "redemptions": points["redemptions"],
            "earned_value": str((Decimal(earned) / per_ils).quantize(Decimal("0.01"))),
            "spent_value": str((Decimal(spent) / per_ils).quantize(Decimal("0.01"))),
            # The liability: points sitting in customers' hands, in shekels.
            # NOT windowed — a balance is a balance, whatever period is on
            # screen — which is exactly why an owner asks about it.
            "outstanding": str(
                (
                    Decimal(
                        models.LoyaltyProfile.objects.for_pharmacy(store_id).aggregate(
                            n=Coalesce(Sum("beans"), Value(0))
                        )["n"]
                        or 0
                    )
                    / per_ils
                ).quantize(Decimal("0.01"))
            ),
            "new_customers": new_customers,
            "identified": identified,
            "repeat": repeat,
            "repeat_rate": round(100 * repeat / identified) if identified else 0,
            "top_customers": top_customers,
        },
    }

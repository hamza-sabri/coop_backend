"""The report tabs, one question each.

    items_report     which drinks sell, which EARN, and which are dead
    item_detail      one drink: when it sells, which size, who buys it
    times_report     by hour × category, and by weekday
    shifts_report    the shifts side by side
    customers_report new vs returning, the regulars, the points
    returns_report   what came back, and why

Every window is in BUSINESS days (see finance.bounds) and every figure is
gross — no VAT. Item revenue is the line total at the price charged on the
line; the bill-level discount and points live on the P&L, not per drink.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Max, Q, Sum, Value
from django.db.models.functions import Coalesce, ExtractHour

from apps.store import finance, models
from apps.store import points as points_service

DEC = DecimalField(max_digits=18, decimal_places=4)
ZERO = Decimal("0")
q2 = finance.q2

WEEK = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def _lines(store_id, start: date, end: date, shift=None):
    lo, hi = finance.bounds(start, end)
    return finance._in_shift(
        models.SaleItem.objects.for_pharmacy(store_id).filter(
            sale__created_at__gte=lo, sale__created_at__lt=hi, sale__is_return=False
        ),
        shift,
        field="sale__created_at",
    )


_COST = ExpressionWrapper(F("unit_cost") * F("quantity"), output_field=DEC)


def _agg():
    return {
        "qty": Coalesce(Sum("quantity"), Value(ZERO, output_field=DEC)),
        "revenue": Coalesce(Sum("line_total"), Value(ZERO, output_field=DEC)),
        "cogs": Coalesce(Sum(_COST, filter=Q(unit_cost__isnull=False)), Value(ZERO, output_field=DEC)),
        "uncosted": Coalesce(Sum("line_total", filter=Q(unit_cost__isnull=True)), Value(ZERO, output_field=DEC)),
        "tickets": Count("sale_id", distinct=True),
    }


def _profit(row) -> Decimal:
    """Profit on lines that HAVE a cost. A line with no cost is not 100%
    margin; it is unknown, and stays out of the profit."""
    return Decimal(row["revenue"]) - Decimal(row["uncosted"]) - Decimal(row["cogs"])


# ── الأصناف ────────────────────────────────────────────────────────────────
def items_report(store_id, start: date, end: date, period="month") -> dict:
    rows = (
        _lines(store_id, start, end)
        .exclude(product_id=None)
        .values("product_id")
        .annotate(**_agg())
    )
    ps, pe = finance.previous_range(start, end, period)
    elapsed_end = min(end, finance.today())
    if elapsed_end < end:
        pe = min(pe, ps + timedelta(days=(elapsed_end - start).days))
    prev = {
        r["product_id"]: r
        for r in _lines(store_id, ps, pe).exclude(product_id=None).values("product_id").annotate(**_agg())
    }
    lo, hi = finance.bounds(start, end)
    returned = {
        r["sale_item__product_id"]: r
        for r in models.SaleReturn.objects.for_pharmacy(store_id)
        .filter(created_at__gte=lo, created_at__lt=hi)
        .values("sale_item__product_id")
        .annotate(n=Coalesce(Sum("quantity"), Value(ZERO, output_field=DEC)), refunds=Sum("refund_amount"))
    }
    last_sold = dict(
        models.SaleItem.objects.for_pharmacy(store_id)
        .filter(sale__is_return=False)
        .exclude(product_id=None)
        .values("product_id")
        .annotate(at=Max("sale__created_at"))
        .values_list("product_id", "at")
    )
    products = {
        p.pk: p
        for p in models.Product.objects.for_pharmacy(store_id).select_related("category")
    }

    by_id = {r["product_id"]: r for r in rows}
    total_rev = sum((Decimal(r["revenue"]) for r in rows), ZERO)
    total_profit = sum((_profit(r) for r in rows), ZERO)
    out = []
    for pid, p in products.items():
        r = by_id.get(pid)
        if r is None and not p.is_active:
            continue
        r = r or {"qty": ZERO, "revenue": ZERO, "cogs": ZERO, "uncosted": ZERO, "tickets": 0}
        profit = _profit(r)
        costed_rev = Decimal(r["revenue"]) - Decimal(r["uncosted"])
        pv = prev.get(pid)
        out.append({
            "product_id": pid,
            "name": p.name,
            "category": p.category.name if p.category_id else "",
            "image": p.image,
            "price": str(p.price),
            "cost": str(p.cost),
            "is_active": p.is_active,
            "qty": str(q2(r["qty"])),
            "prev_qty": str(q2(pv["qty"])) if pv else "0.00",
            "revenue": str(q2(r["revenue"])),
            "cogs": str(q2(r["cogs"])),
            "profit": str(q2(profit)),
            "margin_pct": str(q2(profit * 100 / costed_rev)) if costed_rev else None,
            "revenue_share": str(q2(Decimal(r["revenue"]) * 100 / total_rev)) if total_rev else "0.00",
            "profit_share": str(q2(profit * 100 / total_profit)) if total_profit else "0.00",
            "tickets": r["tickets"],
            "has_cost": Decimal(r["uncosted"]) == 0 and Decimal(p.cost or 0) > 0,
            "returned": str(q2(returned.get(pid, {}).get("n", ZERO))),
            "last_sold_at": last_sold[pid].isoformat() if last_sold.get(pid) else None,
        })
    out.sort(key=lambda x: (-Decimal(x["revenue"]), x["name"]))
    return {
        "items": out,
        "totals": {
            "qty": str(q2(sum((Decimal(r["qty"]) for r in rows), ZERO))),
            "revenue": str(q2(total_rev)),
            "profit": str(q2(total_profit)),
        },
        "previous": {"start": ps.isoformat(), "end": pe.isoformat()},
    }


# ── one drink ─────────────────────────────────────────────────────────────
def item_detail(store_id, product_id: int, start: date, end: date) -> dict | None:
    p = (
        models.Product.objects.for_pharmacy(store_id)
        .select_related("category")
        .filter(pk=product_id)
        .first()
    )
    if p is None:
        return None
    today = finance.today()

    def cups(d1, d2) -> str:
        r = _lines(store_id, d1, d2).filter(product_id=p.pk).aggregate(n=Coalesce(Sum("quantity"), Value(ZERO, output_field=DEC)))
        return str(q2(r["n"]))

    mine = _lines(store_id, start, end).filter(product_id=p.pk)
    head = mine.aggregate(**_agg())
    profit = _profit(head)
    all_rows = _lines(store_id, start, end).values("product_id").annotate(**_agg())
    total_profit = sum((_profit(r) for r in all_rows), ZERO)
    rank = sorted(all_rows, key=lambda r: -Decimal(r["qty"]))
    position = next((i + 1 for i, r in enumerate(rank) if r["product_id"] == p.pk), None)

    series = defaultdict(Decimal)
    for r in mine.values("sale__created_at", "quantity"):
        series[finance.business_date(r["sale__created_at"])] += Decimal(r["quantity"])
    days = []
    d = start
    last = min(end, today)
    while d <= last:
        days.append({"date": d.isoformat(), "qty": str(q2(series.get(d, ZERO)))})
        d += timedelta(days=1)

    by_hour = [
        {"hour": r["h"], "qty": str(q2(r["qty"]))}
        for r in mine.annotate(h=ExtractHour("sale__created_at")).values("h").annotate(
            qty=Coalesce(Sum("quantity"), Value(ZERO, output_field=DEC))
        ).order_by("h")
    ]
    sizes = []
    for r in mine.values("variant_label").annotate(**_agg()).order_by("-qty"):
        sp = _profit(r)
        cr = Decimal(r["revenue"]) - Decimal(r["uncosted"])
        sizes.append({
            "label": r["variant_label"] or "الحجم الأساسي",
            "qty": str(q2(r["qty"])),
            "revenue": str(q2(r["revenue"])),
            "profit": str(q2(sp)),
            "margin_pct": str(q2(sp * 100 / cr)) if cr else None,
        })
    buyers = [
        {"customer_id": r["sale__customer_id"], "name": r["sale__customer__name"], "qty": str(q2(r["qty"]))}
        for r in mine.exclude(sale__customer_id=None)
        .values("sale__customer_id", "sale__customer__name")
        .annotate(qty=Coalesce(Sum("quantity"), Value(ZERO, output_field=DEC)))
        .order_by("-qty")[:5]
    ]
    lo, hi = finance.bounds(start, end)
    rets = (
        models.SaleReturn.objects.for_pharmacy(store_id)
        .filter(sale_item__product_id=p.pk, created_at__gte=lo, created_at__lt=hi)
        .values("reason")
        .annotate(n=Count("id"))
    )
    labels = dict(models.SaleReturn.Reason.choices)
    last_sold = (
        models.SaleItem.objects.for_pharmacy(store_id)
        .filter(product_id=p.pk, sale__is_return=False)
        .aggregate(at=Max("sale__created_at"))["at"]
    )
    costed_rev = Decimal(head["revenue"]) - Decimal(head["uncosted"])
    price, cost = Decimal(p.price or 0), Decimal(p.cost or 0)
    return {
        "product": {
            "id": p.pk, "name": p.name, "price": str(price), "cost": str(cost),
            "category": p.category.name if p.category_id else "",
            "unit_margin_pct": str(q2((price - cost) * 100 / price)) if price and cost else None,
            "unit_profit": str(q2(price - cost)) if cost else None,
        },
        "last_sold_at": last_sold.isoformat() if last_sold else None,
        "cups": {
            "today": cups(today, today),
            "week": cups(today - timedelta(days=6), today),
            "month": cups(today - timedelta(days=29), today),
        },
        "period": {
            "start": start.isoformat(), "end": end.isoformat(),
            "qty": str(q2(head["qty"])), "revenue": str(q2(head["revenue"])),
            "profit": str(q2(profit)), "tickets": head["tickets"],
            "margin_pct": str(q2(profit * 100 / costed_rev)) if costed_rev else None,
            "profit_share": str(q2(profit * 100 / total_profit)) if total_profit else "0.00",
            "rank": position, "of": len(rank),
        },
        "days": days,
        "by_hour": by_hour,
        "sizes": sizes,
        "buyers": buyers,
        "returns": [{"reason": labels.get(r["reason"], r["reason"]), "count": r["n"]} for r in rets],
    }


# ── الأوقات ────────────────────────────────────────────────────────────────
def times_report(store_id, start: date, end: date) -> dict:
    grid = finance.hourly_by_category(store_id, start, end)
    lo, hi = finance.bounds(start, end)
    per_day = defaultdict(lambda: {"revenue": ZERO, "tickets": 0})
    for s in models.Sale.objects.for_pharmacy(store_id).filter(
        created_at__gte=lo, created_at__lt=hi, is_return=False
    ).values("created_at", "discounted_total"):
        d = finance.business_date(s["created_at"])
        per_day[d]["revenue"] += Decimal(s["discounted_total"] or 0)
        per_day[d]["tickets"] += 1
    # Average per weekday over the days that weekday actually occurred in the
    # window — a month has four Mondays and five Fridays, and a sum would
    # crown Friday for that alone.
    counts, revs, tix = defaultdict(int), defaultdict(Decimal), defaultdict(int)
    d = start
    while d <= min(end, finance.today()):
        counts[d.weekday()] += 1
        revs[d.weekday()] += per_day[d]["revenue"] if d in per_day else ZERO
        tix[d.weekday()] += per_day[d]["tickets"] if d in per_day else 0
        d += timedelta(days=1)
    order = [5, 6, 0, 1, 2, 3, 4]  # Saturday first
    weekdays = [
        {
            "weekday": WEEK[w],
            "avg_revenue": str(q2(revs[w] / counts[w])) if counts[w] else "0.00",
            "avg_tickets": str(q2(Decimal(tix[w]) / counts[w])) if counts[w] else "0.00",
            "days": counts[w],
        }
        for w in order
    ]
    best = max(weekdays, key=lambda r: Decimal(r["avg_revenue"])) if weekdays else None
    return {**grid, "weekdays": weekdays, "best_weekday": best["weekday"] if best and Decimal(best["avg_revenue"]) else None}


# ── الورديات ──────────────────────────────────────────────────────────────
def shifts_report(store_id, start: date, end: date) -> dict:
    shifts = list(models.Shift.objects.for_pharmacy(store_id).filter(is_active=True))
    out = []
    for s in shifts:
        p = finance.pnl(store_id, start, end, shift=s, with_series=False, with_compare=False)
        top = (
            _lines(store_id, start, end, shift=s)
            .exclude(product_id=None)
            .values("medication_name")
            .annotate(qty=Sum("quantity"))
            .order_by("-qty")
            .first()
        )
        out.append({
            "id": s.pk, "name": s.name,
            "start": s.start.strftime("%H:%M"), "end": s.end.strftime("%H:%M"),
            "hours": str(s.hours),
            "tickets": p["kpis"]["tickets"],
            "avg_ticket": p["kpis"]["avg_ticket"],
            "net_revenue": p["lines"]["net_revenue"],
            "cogs": p["lines"]["cogs"],
            "gross_profit": p["lines"]["gross_profit"],
            "wages": p["lines"]["shift_wages"],
            "contribution": p["lines"]["contribution"],
            "per_hour": str(q2(Decimal(p["lines"]["net_revenue"]) / s.hours / max(1, p["range"]["days"])))
            if s.hours else "0.00",
            "top_item": top["medication_name"] if top else None,
        })
    total = sum((Decimal(r["net_revenue"]) for r in out), ZERO)
    for r in out:
        r["share"] = str(q2(Decimal(r["net_revenue"]) * 100 / total)) if total else "0.00"
    return {"shifts": out}


# ── الزبائن ────────────────────────────────────────────────────────────────
def customers_report(store_id, start: date, end: date) -> dict:
    lo, hi = finance.bounds(start, end)
    sales = models.Sale.objects.for_pharmacy(store_id).filter(
        created_at__gte=lo, created_at__lt=hi, is_return=False
    )
    tickets = sales.count()
    named = sales.exclude(customer_id=None)
    ids = set(named.values_list("customer_id", flat=True))
    before = set(
        models.Sale.objects.for_pharmacy(store_id)
        .filter(created_at__lt=lo, customer_id__in=ids)
        .values_list("customer_id", flat=True)
    )
    top = list(
        named.values("customer_id", "customer__name", "customer__phone")
        .annotate(spend=Sum("discounted_total"), visits=Count("id"), last=Max("created_at"))
        .order_by("-spend")[:15]
    )
    balances = dict(
        models.LoyaltyProfile.objects.for_pharmacy(store_id)
        .filter(customer_id__in=[t["customer_id"] for t in top])
        .values_list("customer_id", "beans")
    )
    ledger = models.BeanLedger.objects.for_pharmacy(store_id).filter(created_at__gte=lo, created_at__lt=hi)
    earned = ledger.filter(reason="earn").aggregate(n=Coalesce(Sum("delta"), 0))["n"]
    redeemed = -(ledger.filter(reason="redeem").aggregate(n=Coalesce(Sum("delta"), 0))["n"])
    outstanding = (
        models.LoyaltyProfile.objects.for_pharmacy(store_id).aggregate(n=Coalesce(Sum("beans"), 0))["n"]
    )
    added = models.Customer.objects.for_pharmacy(store_id).filter(created_at__gte=lo, created_at__lt=hi).count()
    return {
        "tickets": tickets,
        "identified": named.count(),
        "identified_share": str(q2(Decimal(named.count()) * 100 / tickets)) if tickets else "0.00",
        "customers": len(ids),
        "returning": len(ids & before),
        "new": len(ids - before),
        "added": added,
        "points": {
            "earned": int(earned), "earned_value": str(points_service.value_of(earned)),
            "redeemed": int(redeemed), "redeemed_value": str(points_service.value_of(redeemed)),
            "outstanding": int(outstanding), "outstanding_value": str(points_service.value_of(outstanding)),
        },
        "top": [
            {
                "id": t["customer_id"], "name": t["customer__name"], "phone": t["customer__phone"] or "",
                "spend": str(q2(t["spend"])), "visits": t["visits"],
                "avg": str(q2(Decimal(t["spend"]) / t["visits"])) if t["visits"] else "0.00",
                "last": t["last"].isoformat() if t["last"] else None,
                "points": int(balances.get(t["customer_id"], 0)),
            }
            for t in top
        ],
    }


# ── المرتجعات ─────────────────────────────────────────────────────────────
def returns_report(store_id, start: date, end: date) -> dict:
    lo, hi = finance.bounds(start, end)
    rets = models.SaleReturn.objects.for_pharmacy(store_id).filter(created_at__gte=lo, created_at__lt=hi)
    labels = dict(models.SaleReturn.Reason.choices)
    head = rets.aggregate(
        n=Count("id"),
        refunds=Coalesce(Sum("refund_amount"), Value(ZERO, output_field=DEC)),
        written_off=Coalesce(Sum("cost_written_off"), Value(ZERO, output_field=DEC)),
        remakes=Count("id", filter=Q(refund_amount=0)),
    )
    sold = _lines(store_id, start, end).aggregate(n=Coalesce(Sum("quantity"), Value(ZERO, output_field=DEC)))["n"]
    qty = rets.aggregate(n=Coalesce(Sum("quantity"), Value(ZERO, output_field=DEC)))["n"]
    by_reason = [
        {"reason": r["reason"], "label": labels.get(r["reason"], r["reason"]), "count": r["n"],
         "refunds": str(q2(r["refunds"]))}
        for r in rets.values("reason").annotate(n=Count("id"), refunds=Sum("refund_amount")).order_by("-n")
    ]
    by_item = [
        {"name": r["item_name"], "count": r["n"], "refunds": str(q2(r["refunds"]))}
        for r in rets.values("item_name").annotate(n=Count("id"), refunds=Sum("refund_amount")).order_by("-n")[:10]
    ]
    latest = [
        {
            "id": r.pk, "sale_id": r.sale_id, "item": r.item_name, "reason": labels.get(r.reason, r.reason),
            "note": r.note, "refund": str(r.refund_amount), "quantity": str(r.quantity),
            "by": (r.created_by.get_full_name() or r.created_by.get_username()) if r.created_by_id else "",
            "at": r.created_at.isoformat(),
        }
        for r in rets.select_related("created_by").order_by("-created_at")[:20]
    ]
    return {
        "count": head["n"],
        "remakes": head["remakes"],
        "refunds": str(q2(head["refunds"])),
        "written_off": str(q2(head["written_off"])),
        "rate_pct": str(q2(Decimal(qty) * 100 / Decimal(sold))) if sold else "0.00",
        "by_reason": by_reason,
        "by_item": by_item,
        "latest": latest,
    }

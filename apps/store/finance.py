"""The money questions: did the shop make a profit, and where did it go.

One function, `pnl()`, builds the whole statement for a span of BUSINESS days
(a day runs BUSINESS_DAY_START_HOUR → the same hour next morning, so 01:30 on
Saturday is Friday's takings). Everything here is gross — no VAT anywhere.

The statement, top to bottom:

    gross sales            Σ sale.total                    (before any discount)
  − cashier discounts      Σ (total − discounted_total − points value)
  − points redeemed        Σ value of points spent at the till
  − returns / refunds      Σ SaleReturn.refund_amount (+ legacy return sales)
  = net revenue
  − cost of goods          Σ SaleItem.unit_cost × qty      (frozen at sale time)
  − inventory waste        Σ StockMove(waste).total_cost
  = gross profit
  − operating expenses     by category, prorated by days   (rent, salaries …)
  = net profit

  memo: points outstanding (a liability, never deducted)
  memo: inventory purchases (spend, never cost — COGS already counts usage)

Two rules that keep the numbers honest:

  * Purchases are NOT costs. A crate of milk bought on the 30th is not the
    month's milk cost; the drinks sold are. Booking both would count the
    same milk twice.
  * Stock COUNTS are not losses. Selling a drink never moves raw stock (no
    recipes), so every count shows the milk that went into lattes as a
    shortfall. Only explicit waste is a loss.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import (
    Count, DecimalField, ExpressionWrapper, F, Q, Sum, Value,
)
from django.db.models.functions import Coalesce, TruncTime
from django.utils import timezone

from apps.store import models
from apps.store import points as points_service

DEC = DecimalField(max_digits=18, decimal_places=4)
ZERO = Decimal("0")
CENT = Decimal("0.01")

#: Saturday. The week as it is lived here.
WEEK_START = 5


def q2(x) -> Decimal:
    return Decimal(x or 0).quantize(CENT)


def _hour() -> int:
    return int(getattr(settings, "BUSINESS_DAY_START_HOUR", 4))


def business_date(dt) -> date:
    local = timezone.localtime(dt)
    return local.date() if local.hour >= _hour() else local.date() - timedelta(days=1)


def today() -> date:
    return business_date(timezone.now())


def bounds(start: date, end: date):
    """Aware [from, to) datetimes covering business days start..end."""
    tz = timezone.get_current_timezone()
    h = _hour()
    lo = timezone.make_aware(datetime.combine(start, time(hour=h)), tz)
    hi = timezone.make_aware(datetime.combine(end + timedelta(days=1), time(hour=h)), tz)
    return lo, hi


def _parse(d):
    try:
        return date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return None


def resolve_range(params) -> dict:
    """`period=day|week|month|custom`, anchored on `date` (default: today).

    Returns {start, end, period, label}. `custom` reads `start`/`end` and is
    capped at a year so a typo cannot ask for a decade.
    """
    period = (params.get("period") or "month").lower()
    anchor = _parse(params.get("date")) or today()
    if period == "day":
        start = end = anchor
    elif period == "week":
        start = anchor - timedelta(days=(anchor.weekday() - WEEK_START) % 7)
        end = start + timedelta(days=6)
    elif period == "custom":
        start = _parse(params.get("start")) or anchor
        end = _parse(params.get("end")) or start
        if end < start:
            start, end = end, start
        if (end - start).days > 366:
            start = end - timedelta(days=366)
    else:
        period = "month"
        start = anchor.replace(day=1)
        end = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
    return {"start": start, "end": end, "period": period}


def previous_range(start: date, end: date, period: str):
    if period == "month":
        prev_end = start - timedelta(days=1)
        return prev_end.replace(day=1), prev_end
    span = (end - start).days + 1
    return start - timedelta(days=span), start - timedelta(days=1)


# ── shifts ──────────────────────────────────────────────────────────────────
def _shift_q(shift, field="created_at"):
    """Rows whose LOCAL time of day falls inside the shift."""
    t = f"_t_{field}"
    if shift.crosses_midnight:
        return t, Q(**{f"{t}__gte": shift.start}) | Q(**{f"{t}__lt": shift.end})
    return t, Q(**{f"{t}__gte": shift.start, f"{t}__lt": shift.end})


def _in_shift(qs, shift, field="created_at"):
    if shift is None:
        return qs
    t, q = _shift_q(shift, field)
    return qs.annotate(**{t: TruncTime(field)}).filter(q)


# ── expenses ────────────────────────────────────────────────────────────────
def _months(start: date, end: date):
    m = start.replace(day=1)
    while m <= end:
        yield m
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)


def _overlap_days(month: date, start: date, end: date) -> int:
    last = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    lo, hi = max(month, start), min(last, end)
    return max(0, (hi - lo).days + 1)


def ensure_default_categories(store_id):
    defaults = [
        ("rent", "إيجار"), ("salaries", "رواتب"), ("electricity", "كهرباء"),
        ("water", "مياه"), ("internet", "إنترنت"), ("maintenance", "صيانة"),
        ("marketing", "تسويق"), ("other", "أخرى"),
    ]
    qs = models.ExpenseCategory.objects.for_pharmacy(store_id)
    if qs.exists():
        return
    for i, (key, name) in enumerate(defaults):
        models.ExpenseCategory.objects.get_or_create(
            store_id=store_id, name=name, defaults={"key": key, "position": i}
        )


def opex(store_id, start: date, end: date) -> dict:
    """Operating expenses for business days start..end, by category.

    Each month's costs are prorated by how many of its days fall in the
    window: a day view carries 1/31 of October's rent, a month view all of it.
    """
    by_cat: dict[int, Decimal] = defaultdict(Decimal)
    expenses = models.Expense.objects.for_pharmacy(store_id).filter(
        period__gte=start.replace(day=1), period__lte=end
    )
    per_month: dict[tuple, Decimal] = defaultdict(Decimal)
    for e in expenses.values("period", "category_id", "amount"):
        per_month[(e["period"], e["category_id"])] += Decimal(e["amount"])
    recurring = list(models.RecurringExpense.objects.for_pharmacy(store_id))

    for month in _months(start, end):
        days_in = calendar.monthrange(month.year, month.month)[1]
        frac = Decimal(_overlap_days(month, start, end)) / Decimal(days_in)
        if not frac:
            continue
        for (period, cat), amount in per_month.items():
            if period == month:
                by_cat[cat] += amount * frac
        for r in recurring:
            if r.active_in(month):
                by_cat[r.category_id] += Decimal(r.amount) * frac

    cats = {
        c.pk: c
        for c in models.ExpenseCategory.objects.for_pharmacy(store_id)
    }
    rows = [
        {
            "category_id": cid,
            "name": cats[cid].name if cid in cats else "—",
            "key": cats[cid].key if cid in cats else "",
            "amount": str(q2(amount)),
        }
        for cid, amount in sorted(by_cat.items(), key=lambda kv: -kv[1])
        if q2(amount)
    ]
    total = q2(sum(by_cat.values(), ZERO))
    return {"rows": rows, "total": total}


# ── the statement ───────────────────────────────────────────────────────────
def _sales_block(store_id, lo, hi, shift):
    sales = _in_shift(
        models.Sale.objects.for_pharmacy(store_id).filter(created_at__gte=lo, created_at__lt=hi),
        shift,
    )
    normal = sales.filter(is_return=False)
    head = normal.aggregate(
        gross=Coalesce(Sum("total"), Value(ZERO, output_field=DEC)),
        net=Coalesce(Sum("discounted_total"), Value(ZERO, output_field=DEC)),
        beans=Coalesce(Sum("beans_spent"), 0),
        tickets=Count("id"),
    )
    legacy_returns = sales.filter(is_return=True).aggregate(
        n=Coalesce(Sum("discounted_total"), Value(ZERO, output_field=DEC))
    )["n"]

    items = _in_shift(
        models.SaleItem.objects.for_pharmacy(store_id).filter(
            sale__created_at__gte=lo, sale__created_at__lt=hi, sale__is_return=False
        ),
        shift,
        field="sale__created_at",
    )
    cost_expr = ExpressionWrapper(F("unit_cost") * F("quantity"), output_field=DEC)
    cogs = items.aggregate(
        cogs=Coalesce(Sum(cost_expr, filter=Q(unit_cost__isnull=False)), Value(ZERO, output_field=DEC)),
        uncosted=Coalesce(Sum("line_total", filter=Q(unit_cost__isnull=True)), Value(ZERO, output_field=DEC)),
        uncosted_lines=Count("id", filter=Q(unit_cost__isnull=True)),
        cups=Coalesce(Sum("quantity"), Value(ZERO, output_field=DEC)),
        lines=Count("id"),
    )

    returns = _in_shift(
        models.SaleReturn.objects.for_pharmacy(store_id).filter(created_at__gte=lo, created_at__lt=hi),
        shift,
    ).aggregate(
        refunds=Coalesce(Sum("refund_amount"), Value(ZERO, output_field=DEC)),
        written_off=Coalesce(Sum("cost_written_off"), Value(ZERO, output_field=DEC)),
        n=Count("id"),
        remakes=Count("id", filter=Q(refund_amount=0)),
    )
    return head, legacy_returns, cogs, returns


def pnl(store_id, start: date, end: date, *, shift=None, period="custom",
        with_series=True, with_compare=True) -> dict:
    lo, hi = bounds(start, end)
    head, legacy_returns, cogs_agg, ret = _sales_block(store_id, lo, hi, shift)

    gross = q2(head["gross"])
    net_before_returns = q2(head["net"])
    points_redeemed = q2(points_service.value_of(head["beans"] or 0))
    discounts = q2(gross - net_before_returns - points_redeemed)
    if discounts < 0:
        # A price raised at the till shows up as a negative discount; keep it
        # visible rather than hiding it inside gross.
        discounts = q2(discounts)
    refunds = q2(ret["refunds"]) + q2(legacy_returns)
    net_revenue = q2(net_before_returns - refunds)
    cogs = q2(cogs_agg["cogs"])

    waste_qs = _in_shift(
        models.StockMove.objects.for_pharmacy(store_id).filter(
            kind=models.StockMove.Kind.WASTE, created_at__gte=lo, created_at__lt=hi
        ),
        shift,
    )
    waste = q2(waste_qs.aggregate(n=Coalesce(Sum("total_cost"), Value(ZERO, output_field=DEC)))["n"])
    gross_profit = q2(net_revenue - cogs - waste)

    # Days actually traded so far: a month view on the 10th prorates ten days
    # of rent, not thirty-one, or every month looks like a loss until the end.
    elapsed_end = min(end, today())
    days = max(0, (elapsed_end - start).days + 1)

    out = {
        "range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "elapsed_end": elapsed_end.isoformat(),
            "days": days,
            "period": period,
            "day_start_hour": _hour(),
        },
        "shift": None,
        "lines": {
            "gross_sales": str(gross),
            "discounts": str(discounts),
            "points_redeemed": str(points_redeemed),
            "returns": str(refunds),
            "net_revenue": str(net_revenue),
            "cogs": str(cogs),
            "waste": str(waste),
            "gross_profit": str(gross_profit),
        },
        "kpis": {
            "tickets": head["tickets"],
            "cups": str(q2(cogs_agg["cups"])),
            "avg_ticket": str(q2(net_before_returns / head["tickets"])) if head["tickets"] else "0.00",
            "cost_per_cup": str(q2(cogs / cogs_agg["cups"])) if cogs_agg["cups"] else "0.00",
            "gross_margin_pct": str(q2(gross_profit * 100 / net_revenue)) if net_revenue else "0.00",
            "returns_count": ret["n"],
            "remakes": ret["remakes"],
            "returns_cost_written_off": str(q2(ret["written_off"])),
        },
        "coverage": {
            # Revenue with no cost behind it — sold before costs were entered.
            "uncosted_revenue": str(q2(cogs_agg["uncosted"])),
            "uncosted_lines": cogs_agg["uncosted_lines"],
            "lines": cogs_agg["lines"],
        },
        "memo": {
            "points_outstanding": None,
            "points_outstanding_value": None,
            "purchases": None,
        },
    }

    if shift is not None:
        wages = q2(Decimal(shift.wage_per_day or 0) * days)
        out["shift"] = {
            "id": shift.pk, "name": shift.name,
            "start": shift.start.strftime("%H:%M"), "end": shift.end.strftime("%H:%M"),
            "wage_per_day": str(shift.wage_per_day),
        }
        out["lines"]["shift_wages"] = str(wages)
        out["lines"]["contribution"] = str(q2(gross_profit - wages))
    else:
        ex = opex(store_id, start, elapsed_end) if days else {"rows": [], "total": ZERO}
        out["opex"] = ex["rows"]
        out["lines"]["opex"] = str(ex["total"])
        net_profit = q2(gross_profit - ex["total"])
        out["lines"]["net_profit"] = str(net_profit)
        # The daily takings that would cover the fixed costs at this margin.
        margin = (gross_profit / net_revenue) if net_revenue else ZERO
        out["kpis"]["break_even_daily"] = (
            str(q2((ex["total"] / days) / margin)) if (days and margin > 0) else None
        )
        out["kpis"]["net_margin_pct"] = (
            str(q2(net_profit * 100 / net_revenue)) if net_revenue else "0.00"
        )

    # Memo lines — owed and spent, never deducted.
    from apps.store.models import LoyaltyProfile

    outstanding = (
        LoyaltyProfile.objects.for_pharmacy(store_id).aggregate(n=Coalesce(Sum("beans"), 0))["n"] or 0
    )
    out["memo"]["points_outstanding"] = int(outstanding)
    out["memo"]["points_outstanding_value"] = str(points_service.value_of(outstanding))
    purchases = models.StockMove.objects.for_pharmacy(store_id).filter(
        kind=models.StockMove.Kind.PURCHASE, created_at__gte=lo, created_at__lt=hi
    ).aggregate(n=Coalesce(Sum("total_cost"), Value(ZERO, output_field=DEC)))["n"]
    out["memo"]["purchases"] = str(q2(purchases))

    if with_series:
        out["series"] = _series(store_id, start, end, shift)

    if with_compare:
        ps, pe = previous_range(start, end, period)
        if elapsed_end < end and days:
            # A period still running is compared like for like: the first 4
            # days of October against the first 4 days of September, not
            # against all of September.
            pe = min(pe, ps + timedelta(days=days - 1))
        prev = pnl(store_id, ps, pe, shift=shift, period=period,
                   with_series=False, with_compare=False)
        out["previous"] = {
            "start": ps.isoformat(), "end": pe.isoformat(),
            "net_revenue": prev["lines"]["net_revenue"],
            "gross_profit": prev["lines"]["gross_profit"],
            "net_profit": prev["lines"].get("net_profit"),
            "contribution": prev["lines"].get("contribution"),
            "tickets": prev["kpis"]["tickets"],
        }
    return out


def _series(store_id, start: date, end: date, shift) -> list:
    """Per business day: net revenue, COGS, gross profit — and, store-wide,
    that day's share of the month's expenses and the net."""
    lo, hi = bounds(start, end)
    rev = defaultdict(Decimal)
    cost = defaultdict(Decimal)
    tickets = defaultdict(int)

    sales = _in_shift(
        models.Sale.objects.for_pharmacy(store_id).filter(created_at__gte=lo, created_at__lt=hi),
        shift,
    )
    for s in sales.values("created_at", "discounted_total", "is_return"):
        d = business_date(s["created_at"])
        if s["is_return"]:
            rev[d] -= Decimal(s["discounted_total"] or 0)
        else:
            rev[d] += Decimal(s["discounted_total"] or 0)
            tickets[d] += 1
    for r in _in_shift(
        models.SaleReturn.objects.for_pharmacy(store_id).filter(created_at__gte=lo, created_at__lt=hi),
        shift,
    ).values("created_at", "refund_amount"):
        rev[business_date(r["created_at"])] -= Decimal(r["refund_amount"] or 0)
    items = _in_shift(
        models.SaleItem.objects.for_pharmacy(store_id).filter(
            sale__created_at__gte=lo, sale__created_at__lt=hi,
            sale__is_return=False, unit_cost__isnull=False,
        ),
        shift, field="sale__created_at",
    )
    for i in items.values("sale__created_at", "unit_cost", "quantity"):
        cost[business_date(i["sale__created_at"])] += Decimal(i["unit_cost"]) * Decimal(i["quantity"])
    for w in _in_shift(
        models.StockMove.objects.for_pharmacy(store_id).filter(
            kind=models.StockMove.Kind.WASTE, created_at__gte=lo, created_at__lt=hi
        ),
        shift,
    ).values("created_at", "total_cost"):
        cost[business_date(w["created_at"])] += Decimal(w["total_cost"] or 0)

    last = min(end, today())
    month_cost: dict[date, Decimal] = {}
    out = []
    d = start
    while d <= end:
        row = {
            "date": d.isoformat(),
            "tickets": tickets.get(d, 0),
            "net_revenue": str(q2(rev.get(d, ZERO))),
            "cogs": str(q2(cost.get(d, ZERO))),
            "gross_profit": str(q2(rev.get(d, ZERO) - cost.get(d, ZERO))),
        }
        if shift is None and d <= last:
            m = d.replace(day=1)
            if m not in month_cost:
                last_day = m.replace(day=calendar.monthrange(m.year, m.month)[1])
                month_cost[m] = opex(store_id, m, last_day)["total"] / Decimal(last_day.day)
            daily = q2(month_cost[m])
            row["opex"] = str(daily)
            row["net_profit"] = str(q2(rev.get(d, ZERO) - cost.get(d, ZERO) - daily))
        elif shift is not None:
            row["shift_wages"] = str(q2(shift.wage_per_day or 0)) if d <= last else "0.00"
        out.append(row)
        d += timedelta(days=1)
    return out


# ── when, and what ──────────────────────────────────────────────────────────
def hourly_by_category(store_id, start: date, end: date, shift=None) -> dict:
    """Cups and revenue per local hour × category: when the cold drinks sell
    and when the hot ones do."""
    from django.db.models.functions import ExtractHour

    lo, hi = bounds(start, end)
    items = _in_shift(
        models.SaleItem.objects.for_pharmacy(store_id).filter(
            sale__created_at__gte=lo, sale__created_at__lt=hi, sale__is_return=False
        ),
        shift, field="sale__created_at",
    )
    rows = (
        items.annotate(h=ExtractHour("sale__created_at"))
        .values("h", "category")
        .annotate(
            qty=Coalesce(Sum("quantity"), Value(ZERO, output_field=DEC)),
            revenue=Coalesce(Sum("line_total"), Value(ZERO, output_field=DEC)),
        )
    )
    cats: dict[str, Decimal] = defaultdict(Decimal)
    cells = []
    hours: dict[int, Decimal] = defaultdict(Decimal)
    for r in rows:
        name = r["category"] or "بلا تصنيف"
        cats[name] += Decimal(r["revenue"])
        hours[r["h"]] += Decimal(r["qty"])
        cells.append({
            "hour": r["h"], "category": name,
            "qty": str(q2(r["qty"])), "revenue": str(q2(r["revenue"])),
        })
    peak = max(hours.items(), key=lambda kv: kv[1])[0] if hours else None
    return {
        "categories": [c for c, _ in sorted(cats.items(), key=lambda kv: -kv[1])],
        "cells": cells,
        "peak_hour": peak,
        "day_start_hour": _hour(),
    }

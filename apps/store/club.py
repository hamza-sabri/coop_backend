"""The customer's own page on the web — profile, points, visits.

Used by /shop/me/ (GET adds `profile` and `purchases`, PATCH saves the
profile). Ordering and the menu stay with the Flutter app; this is the light
web door that gets customers signed in and tells the shop who they are.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Sum

from apps.store import models

PHONE_RE = re.compile(r"^0\d{8,9}$")


def normalise_phone(raw: str) -> str:
    """Digits only, local form: +970 59… / 00972 59… / 59… → 059…"""
    d = re.sub(r"\D", "", raw or "")
    for cc in ("00970", "00972", "970", "972"):
        if d.startswith(cc) and len(d) > len(cc) + 7:
            d = "0" + d[len(cc):].lstrip("0")
            break
    if d and not d.startswith("0") and len(d) == 9:
        d = "0" + d
    return d


def profile(c: models.Customer) -> dict:
    from apps.core.uploads import resolve_stored_url

    return {
        "name": c.name,
        "email": c.email,
        "phone": c.phone or "",
        "gender": c.gender if c.gender_set else "",
        "birth_date": c.birth_date.isoformat() if c.birth_date else "",
        "avatar": resolve_stored_url(c.avatar),
        "member_since": c.created_at.date().isoformat(),
        "complete": bool(c.phone and c.gender_set),
    }


def stats(store, c: models.Customer) -> dict:
    """The customer's own numbers for their account dashboard: how often they
    come, what they spend, what they have most — over ALL their visits, not
    just the last page of `purchases`."""
    from django.db.models import Count
    from django.db.models.functions import ExtractHour, TruncMonth
    from django.utils import timezone

    sales = models.Sale.objects.for_pharmacy(store.pk).filter(customer=c, is_return=False)
    agg = sales.aggregate(n=Count("id"), spent=Sum("discounted_total"))
    visits = agg["n"] or 0
    spent = agg["spent"] or 0
    first = sales.order_by("created_at").values_list("created_at", flat=True).first()
    last = sales.order_by("-created_at").values_list("created_at", flat=True).first()

    items = models.SaleItem.objects.for_pharmacy(store.pk).filter(sale__in=sales)
    top = list(
        items.exclude(medication_name="").values("medication_name")
        .annotate(q=Sum("quantity")).order_by("-q", "medication_name")[:3]
    )
    cats = list(
        items.exclude(category="").values("category")
        .annotate(q=Sum("quantity")).order_by("-q", "category")[:3]
    )

    tz = timezone.get_current_timezone()
    now = timezone.localtime()
    start = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=150)).replace(day=1)
    by_month = {
        r["m"].date().isoformat()[:7]: r
        for r in sales.filter(created_at__gte=start)
        .annotate(m=TruncMonth("created_at", tzinfo=tz)).values("m")
        .annotate(n=Count("id"), s=Sum("discounted_total"))
    }
    months, y, m = [], start.year, start.month
    for _ in range(6):
        key = f"{y:04d}-{m:02d}"
        r = by_month.get(key)
        months.append({"month": key, "visits": r["n"] if r else 0, "spent": str(r["s"] or 0) if r else "0"})
        m += 1
        if m > 12:
            y, m = y + 1, 1

    hour = (
        sales.annotate(h=ExtractHour("created_at", tzinfo=tz)).values("h")
        .annotate(n=Count("id")).order_by("-n", "h").values_list("h", flat=True).first()
    )
    orders = models.Order.objects.for_pharmacy(store.pk).filter(customer=c).exclude(status="cancelled").count()

    def qty(v):
        v = v or 0
        return int(v) if v == int(v) else float(v)

    return {
        "visits": visits,
        "spent": str(spent),
        "avg_bill": str(round(spent / visits, 2)) if visits else "0",
        "first_visit": timezone.localtime(first).isoformat() if first else None,
        "last_visit": timezone.localtime(last).isoformat() if last else None,
        "top_items": [{"name": r["medication_name"], "qty": qty(r["q"])} for r in top],
        "top_categories": [{"name": r["category"], "qty": qty(r["q"])} for r in cats],
        "months": months,
        "usual_hour": hour,
        "app_orders": orders,
    }


def purchases(store, c: models.Customer, limit: int = 30) -> list[dict]:
    """Their visits at the till, newest first, with what they had and the
    points each one earned."""
    sales = list(
        models.Sale.objects.for_pharmacy(store.pk)
        .filter(customer=c)
        .order_by("-created_at")
        .prefetch_related("items__product", "items__variant")[:limit]
    )
    earned = dict(
        models.BeanLedger.objects.for_pharmacy(store.pk)
        .filter(sale_id__in=[s.pk for s in sales], delta__gt=0)
        .values_list("sale_id")
        .annotate(n=Sum("delta"))
    )
    out = []
    for s in sales:
        out.append({
            "id": s.pk,
            "code": s.receipt_code,
            "at": s.created_at.isoformat(),
            "is_return": s.is_return,
            "total": str(s.discounted_total),
            # The bill's breakdown, for the visit's detail sheet.
            "subtotal": str(s.total),
            "points_value": str(s.beans_value),
            "payment": s.payment_method,
            "points_spent": s.beans_spent,
            "points_earned": int(earned.get(s.pk) or 0),
            "items": [
                {
                    "name": (i.medication_name + (f" — {i.variant_label}" if i.variant_label else "")).strip(),
                    "qty": str(i.quantity.normalize()),
                    "price": str(i.unit_price),
                    "line_total": str(i.line_total),
                    # The drink's picture (the size's own, if it has one).
                    # "b2://" keys are signed on the way out by the renderer.
                    "image": (
                        (getattr(i.variant, "image", "") if i.variant_id else "")
                        or (i.product.image if i.product_id else "")
                        or ""
                    ),
                }
                for i in s.items.all()
            ],
        })
    return out


class ProfileError(Exception):
    def __init__(self, field: str, msg: str, status: int = 400):
        super().__init__(msg)
        self.field, self.msg, self.status = field, msg, status


def _merge_into(store, app_row: models.Customer, till_row: models.Customer) -> models.Customer:
    """The number they typed belongs to a customer the staff already know
    (added at the till, never signed in). That is the same person: move the
    app account onto that row so their visits and points are one history."""
    for model in (models.Sale, models.Debt, models.Order, models.BeanLedger,
                  models.Notification, models.PushSubscription, models.DeviceToken):
        model.unguarded.filter(customer=app_row).update(customer=till_row)

    till_row.firebase_uid = till_row.firebase_uid or app_row.firebase_uid
    till_row.clerk_id = till_row.clerk_id or app_row.clerk_id
    till_row.email = till_row.email or app_row.email
    till_row.avatar = till_row.avatar or app_row.avatar
    if app_row.gender_set:
        till_row.gender, till_row.gender_set = app_row.gender, True
    till_row.birth_date = till_row.birth_date or app_row.birth_date
    if not till_row.name.strip():
        till_row.name = app_row.name

    # One balance: whatever the ledger now says for the merged row.
    models.LoyaltyProfile.unguarded.filter(customer=app_row).delete()
    app_row.firebase_uid = ""
    app_row.clerk_id = ""
    app_row.save(update_fields=["firebase_uid", "clerk_id"])
    app_row.delete()
    till_row.save()
    balance = (
        models.BeanLedger.unguarded.filter(customer=till_row).aggregate(n=Sum("delta"))["n"] or 0
    )
    prof, _ = models.LoyaltyProfile.unguarded.get_or_create(
        store=store, customer=till_row, defaults={"beans": 0}
    )
    prof.beans = max(0, int(balance))
    prof.save(update_fields=["beans"])
    return till_row


@transaction.atomic
def update_profile(store, c: models.Customer, data: dict) -> models.Customer:
    """Save what the customer typed. Returns the row now holding their account
    (a different row when their number joined them to an existing customer)."""
    fields = []
    if "name" in data:
        name = " ".join(str(data.get("name") or "").split())[:255]
        if not name:
            raise ProfileError("name", "اكتب اسمك.")
        c.name = name
        fields.append("name")
    if "gender" in data:
        g = str(data.get("gender") or "")
        if g not in ("male", "female"):
            raise ProfileError("gender", "اختر ذكر أو أنثى.")
        c.gender, c.gender_set = g, True
        fields += ["gender", "gender_set"]
    if "birth_date" in data:
        raw = str(data.get("birth_date") or "").strip()
        if raw:
            try:
                bd = date.fromisoformat(raw)
            except ValueError:
                raise ProfileError("birth_date", "تاريخ غير صحيح.")
            if not (1920 <= bd.year <= date.today().year - 5):
                raise ProfileError("birth_date", "تاريخ غير صحيح.")
            c.birth_date = bd
        else:
            c.birth_date = None
        fields.append("birth_date")
    if fields:
        c.save(update_fields=list(dict.fromkeys(fields)) + ["updated_at"])

    if "phone" in data:
        phone = normalise_phone(str(data.get("phone") or ""))
        if not PHONE_RE.match(phone):
            raise ProfileError("phone", "رقم غير صحيح — مثال: 0597020201")
        if phone != (c.phone or ""):
            other = (
                models.Customer.objects.for_pharmacy(store.pk)
                .filter(phone=phone).exclude(pk=c.pk).first()
            )
            if other is not None:
                if other.firebase_uid or other.clerk_id:
                    raise ProfileError("phone", "هذا الرقم مربوط بحساب آخر.", status=409)
                return _merge_into(store, c, other)
            c.phone = phone
            c.save(update_fields=["phone", "updated_at"])
    return c

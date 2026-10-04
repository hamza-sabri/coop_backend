"""Fill a shop with believable trade so every screen has something to show.

    python manage.py seed_showcase                    # كوب, last 60 days
    python manage.py seed_showcase --days 90 --store koup
    python manage.py seed_showcase --staff-password demo12345

and when the show is over:

    python manage.py purge_showcase --yes

What it makes, all of it registered in DemoMark so the purge removes exactly
this and nothing the shop entered itself:

  * customers with points earned, spent and partly reversed by returns
  * ~two months of sales shaped like a café day — quiet at opening, a rush
    16:00–18:00 and another 21:00–23:00, busier Thursday/Friday nights
  * returns with reasons, including remakes
  * raw materials (cups, milk, beans, fruit …) with purchases, waste and
    weekly counts
  * three months of expenses: rent, salaries and internet as recurring
    costs; electricity, water, maintenance and marketing as monthly bills
  * two shifts (صباحي 13:00–19:00, مسائي 19:00–02:00) and two demo staff
  * a cost on every drink that has none (put back to zero on purge)

Points bands are settings, not data: if the shop has none, the four default
bands are created and LEFT in place by the purge.

Sales are written through the ORM rather than the API so 5,000 of them take
seconds, but points go through the real ledger functions — the demo moves
balances exactly the way the till does.
"""
from __future__ import annotations

import random
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.store import finance, models
from apps.store import points as points_service

FIRST_M = ["أحمد", "محمد", "عمر", "يوسف", "خالد", "سامر", "باسل", "زياد", "مراد",
           "طارق", "أنس", "رامي", "وسيم", "نادر", "هاني", "إياد", "قيس", "فادي"]
FIRST_F = ["سارة", "ليان", "دانا", "رند", "مريم", "نور", "هبة", "رنا", "لمى",
           "جنى", "ياسمين", "سلمى", "تالا", "بيان", "شهد", "زينة", "آية", "ملك"]
LAST = ["عبد الله", "أبو زيد", "الحاج", "دعمس", "زيد", "صبري", "عودة", "نزال",
        "شواهنة", "مرعي", "الخطيب", "قاسم", "حمد", "طه", "ناصر", "عمران"]

#: Relative traffic by LOCAL hour. The shop opens at 13:00 and closes ~02:00.
HOURLY = {13: 0.35, 14: 0.45, 15: 0.6, 16: 0.95, 17: 1.0, 18: 0.85, 19: 0.6,
          20: 0.75, 21: 0.95, 22: 1.0, 23: 0.8, 0: 0.5, 1: 0.25}
#: Python weekday → multiplier. Thursday and Friday nights are the week.
WEEKDAY = {0: 0.85, 1: 0.8, 2: 0.85, 3: 1.15, 4: 1.25, 5: 1.0, 6: 0.9}

RETURN_REASONS = [("wrong_order", 4), ("taste", 3), ("late", 1), ("spilled", 3),
                  ("changed_mind", 2), ("other", 1)]

#: name, category, purchase_unit, purchase_qty, purchase_cost, daily use (base
#: units), reorder level (base), shelf life days (None = no expiry)
MATERIALS = [
    ("أكواب ورق 12oz", "تغليف", "piece", 100, 10, 55, 300, None),
    ("أغطية أكواب", "تغليف", "piece", 100, 6, 55, 300, None),
    ("كاسات بلاستيك للبارد", "تغليف", "piece", 50, 9, 30, 150, None),
    ("مصاصات", "تغليف", "piece", 200, 8, 30, 200, None),
    ("محارم", "تغليف", "piece", 500, 12, 70, 400, None),
    ("حليب كامل الدسم", "ألبان", "l", 1, 5, 9000, 10000, 7),
    ("حليب شوفان", "ألبان", "l", 1, 14, 900, 2000, 30),
    ("كريمة خفق", "ألبان", "l", 1, 18, 300, 1000, 14),
    ("بن إسبريسو", "قهوة", "kg", 1, 95, 700, 2000, 120),
    ("كاكاو", "قهوة", "kg", 1, 45, 120, 500, 180),
    ("سيرب كراميل", "سيرب", "l", 1, 38, 150, 500, 180),
    ("سيرب بندق", "سيرب", "l", 1, 38, 120, 500, 180),
    ("سكر", "جاف", "kg", 1, 4, 600, 3000, None),
    ("موز", "فواكه", "kg", 1, 10, 900, 2000, 5),
    ("فراولة مجمدة", "فواكه", "kg", 1, 22, 500, 1500, 90),
    ("برتقال للعصير", "فواكه", "kg", 1, 6, 1500, 4000, 10),
    ("جوافة", "فواكه", "kg", 1, 12, 600, 1500, 7),
    ("بودرة بروتين", "جاف", "kg", 1, 120, 150, 500, 365),
]

EXPENSES_RECURRING = [("rent", "إيجار المحل", Decimal("3000")),
                      ("salaries", "رواتب الموظفين", Decimal("7500")),
                      ("internet", "إنترنت", Decimal("150"))]
EXPENSES_MONTHLY = [("electricity", 1100, 1600), ("water", 90, 180)]
EXPENSES_SOMETIMES = [("maintenance", 150, 700, 0.6), ("marketing", 200, 600, 0.7),
                      ("other", 50, 250, 0.8)]

DEFAULT_BANDS = [(Decimal("0"), Decimal("20"), Decimal("1")),
                 (Decimal("20"), Decimal("50"), Decimal("2")),
                 (Decimal("50"), Decimal("100"), Decimal("5")),
                 (Decimal("100"), None, Decimal("5"))]

SHIFTS = [("صباحي", time(13), time(19), Decimal("140")),
          ("مسائي", time(19), time(2), Decimal("180"))]


class Marker:
    """Collects (model, pk) pairs and writes them to DemoMark in bulk."""

    def __init__(self, store):
        self.store = store
        self.rows: list[models.DemoMark] = []

    def add(self, obj, model=None, restore=None):
        label = model or f"{obj._meta.app_label}.{obj.__class__.__name__}"
        self.rows.append(models.DemoMark(
            store=self.store, model=label, object_pk=obj.pk, restore=restore
        ))

    def flush(self):
        if self.rows:
            models.DemoMark.objects.bulk_create(self.rows, ignore_conflicts=True)
            self.rows = []


@contextmanager
def frozen(when):
    """Make auto_now_add stamp `when` — the ORM reads timezone.now()."""
    with mock.patch("django.utils.timezone.now", return_value=when):
        yield


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Command(BaseCommand):
    help = "Seed showcase data (sales, returns, inventory, expenses) — removable with purge_showcase."

    def add_arguments(self, parser):
        parser.add_argument("--store", default="koup")
        parser.add_argument("--days", type=int, default=60)
        parser.add_argument("--customers", type=int, default=45)
        parser.add_argument("--seed", type=int, default=7, help="random seed (same data each run)")
        parser.add_argument("--staff-password", default="",
                            help="password for the two demo staff (default: they cannot log in)")

    def handle(self, *args, **o):
        store = models.Store.objects.filter(slug=o["store"]).first()
        if store is None:
            raise CommandError(f"no store with slug {o['store']!r}")
        if models.DemoMark.objects.for_pharmacy(store).exists():
            raise CommandError(
                "this shop already has showcase data — run `purge_showcase --yes` first"
            )
        products = list(
            models.Product.objects.for_pharmacy(store)
            .filter(is_active=True).select_related("category").prefetch_related("variants")
        )
        if not products:
            raise CommandError("the menu is empty — seed the menu first")

        self.rng = random.Random(o["seed"])
        self.store = store
        self.mark = Marker(store)
        days = max(7, min(int(o["days"]), 365))
        today = finance.today()
        start = today - timedelta(days=days - 1)

        self.stdout.write(f"showcase for {store.name}: {start} → {today}")
        with transaction.atomic():
            self._costs(products)
            self._bands()
            shifts = self._shifts()
            staff = self._staff(o["staff_password"])
            customers = self._customers(int(o["customers"]))
            self.mark.flush()
        n_sales, n_returns = self._sales(products, customers, staff, start, today)
        with transaction.atomic():
            n_moves = self._inventory(start, today, staff)
            n_exp = self._expenses(start, today)
            self.mark.flush()

        from apps.store.views import (
            invalidate_reports_cache, invalidate_sales_stats_cache,
        )
        invalidate_reports_cache(store.pk)
        invalidate_sales_stats_cache(store.pk)
        self.stdout.write(self.style.SUCCESS(
            f"done: {len(customers)} customers, {n_sales} sales, {n_returns} returns, "
            f"{n_moves} stock moves, {n_exp} expenses, {len(shifts)} shifts, {len(staff)} staff"
        ))
        self.stdout.write("remove it all with: python manage.py purge_showcase --yes")

    # ── setup ────────────────────────────────────────────────────────────
    def _costs(self, products):
        """Give every drink without a cost a believable one (28–38% of price)."""
        for p in products:
            if not p.cost or p.cost <= 0:
                old = str(p.cost or "0")
                p.cost = money(Decimal(p.price or 0) * Decimal(str(self.rng.uniform(0.28, 0.38))))
                models.Product.objects.for_pharmacy(self.store).filter(pk=p.pk).update(cost=p.cost)
                self.mark.add(p, model="cost:store.Product", restore={"cost": old})
            for v in p.variants.all():
                if not v.cost or v.cost <= 0:
                    old = str(v.cost or "0")
                    price = Decimal(v.price or p.price or 0)
                    v.cost = money(price * Decimal(str(self.rng.uniform(0.28, 0.38))))
                    models.ProductVariant.objects.for_pharmacy(self.store).filter(pk=v.pk).update(cost=v.cost)
                    self.mark.add(v, model="cost:store.ProductVariant", restore={"cost": old})

    def _bands(self):
        if models.EarnRule.objects.for_pharmacy(self.store).exists():
            return
        for i, (lo, hi, rate) in enumerate(DEFAULT_BANDS):
            models.EarnRule.objects.create(
                store=self.store, min_total=lo, max_total=hi, rate_percent=rate, position=i
            )
        self.stdout.write("  points bands: defaults created (kept on purge)")

    def _shifts(self):
        existing = list(models.Shift.objects.for_pharmacy(self.store))
        if existing:
            return existing
        out = []
        for i, (name, a, b, wage) in enumerate(SHIFTS):
            s = models.Shift.objects.create(
                store=self.store, name=name, start=a, end=b, wage_per_day=wage, position=i
            )
            self.mark.add(s)
            out.append(s)
        return out

    def _staff(self, password):
        User = get_user_model()
        out = list(User.objects.filter(store=self.store, is_active=True))
        for uname, first in (("demo-layan", "ليان"), ("demo-anas", "أنس")):
            if User.objects.filter(username=uname).exists():
                continue
            u = User(username=uname, first_name=first, display_name=first,
                     store=self.store, role="employee")
            if password:
                u.set_password(password)
            else:
                u.set_unusable_password()
            u.save()
            self.mark.add(u)
            out.append(u)
        return out

    def _customers(self, n):
        out = []
        used = set(
            models.Customer.objects.for_pharmacy(self.store)
            .exclude(phone=None).values_list("phone", flat=True)
        )
        for i in range(n):
            female = self.rng.random() < 0.45
            first = self.rng.choice(FIRST_F if female else FIRST_M)
            name = f"{first} {self.rng.choice(LAST)}"
            phone = None
            for _ in range(20):
                cand = f"059{self.rng.randint(1000000, 9999999)}"
                if cand not in used:
                    phone = cand
                    used.add(cand)
                    break
            c = models.Customer.objects.create(
                store=self.store, name=name, phone=phone,
                gender="female" if female else "male",
            )
            self.mark.add(c)
            out.append(c)
        return out

    # ── trade ────────────────────────────────────────────────────────────
    def _ticket_lines(self, products):
        # Coffee sells most; a box of breakfast rarely.
        weights = []
        for p in products:
            cat = (p.category.name if p.category_id else "")
            w = 3.0 if "قهوة" in cat else 1.6 if ("سموذي" in cat or "عصائر" in cat) else 1.0
            if Decimal(p.price or 0) > 50:
                w = 0.25
            weights.append(w)
        n = self.rng.choices([1, 2, 3, 4], weights=[62, 27, 9, 2])[0]
        lines = []
        for p in self.rng.choices(products, weights=weights, k=n):
            variants = [v for v in p.variants.all() if v.is_active]
            v = self.rng.choice(variants) if variants and self.rng.random() < 0.8 else None
            qty = 2 if self.rng.random() < 0.08 else 1
            lines.append((p, v, qty))
        return lines

    def _sales(self, products, customers, staff, start, today):
        tz = timezone.get_current_timezone()
        now = timezone.now()
        hour0 = finance._hour()
        sale_marks = []
        n_returns = 0
        regulars = customers[: max(1, len(customers) // 3)]
        sold = 0
        d = start
        while d <= today:
            base = self.rng.uniform(30, 40) * WEEKDAY[d.weekday()]
            # A gentle upward trend, so "this month vs last" has a story.
            base *= 0.9 + 0.2 * ((d - start).days / max(1, (today - start).days))
            for hour, weight in HOURLY.items():
                day = d if hour >= hour0 else d + timedelta(days=1)
                n = int(self.rng.gauss(base * weight / 8.7, 1.2) + 0.5)
                for _ in range(max(0, n)):
                    when = timezone.make_aware(
                        datetime.combine(day, time(hour, self.rng.randint(0, 59), self.rng.randint(0, 59))), tz
                    )
                    if when >= now:
                        continue
                    sale = self._one_sale(products, customers, regulars, staff, when)
                    if sale is None:
                        continue
                    sold += 1
                    sale_marks.append(models.DemoMark(
                        store=self.store, model="store.Sale", object_pk=sale.pk
                    ))
                    if self.rng.random() < 0.018:
                        n_returns += self._return(sale, when, staff)
            if len(sale_marks) > 500:
                models.DemoMark.objects.bulk_create(sale_marks, ignore_conflicts=True)
                sale_marks = []
            d += timedelta(days=1)
        models.DemoMark.objects.bulk_create(sale_marks, ignore_conflicts=True)
        self.mark.flush()
        return sold, n_returns

    def _one_sale(self, products, customers, regulars, staff, when):
        lines = self._ticket_lines(products)
        r = self.rng.random()
        customer = None
        if r < 0.28:
            customer = self.rng.choice(regulars)
        elif r < 0.42:
            customer = self.rng.choice(customers)
        with frozen(when), transaction.atomic():
            sale = None
            for _ in range(5):
                try:
                    with transaction.atomic():
                        sale = models.Sale.objects.create(
                            store=self.store, customer=customer, payment_method="cash",
                            receipt_code=models.Sale.new_receipt_code(when),
                            created_by=self.rng.choice(staff) if staff else None,
                        )
                    break
                except IntegrityError:
                    sale = None
            if sale is None:
                return None
            items = []
            for p, v, qty in lines:
                price = Decimal((v.price if v and v.price else p.price) or 0)
                cost = (v.cost if v and v.cost and v.cost > 0 else p.cost) or None
                items.append(models.SaleItem(
                    sale=sale, product=p, variant=v,
                    medication_name=p.name, variant_label=v.label if v else "",
                    category=p.category.name if p.category_id else "",
                    unit_price=price, quantity=Decimal(qty),
                    unit_cost=Decimal(cost) if cost and Decimal(cost) > 0 else None,
                    line_total=money(price * qty),
                ))
            models.SaleItem.objects.bulk_create(items)
            total = sum((i.line_total for i in items), Decimal("0.00"))
            sale.total = total
            sale.discounted_total = total
            # A round-down at the till now and then: 23.50 → 23.
            if self.rng.random() < 0.05 and total > 10:
                sale.discounted_total = Decimal(int(total))
                if sale.discounted_total == total:
                    sale.discounted_total = total - 1
            if customer is not None:
                # Fresh row: the cached `loyalty` on a reused object would
                # hold the balance from the first visit forever.
                customer = models.Customer.objects.for_pharmacy(self.store).get(pk=customer.pk)
                bal = points_service.balance_of(customer)
                if bal >= 40 and self.rng.random() < 0.5:
                    want = min(bal, self.rng.choice([40, 60, 100]))
                    spent = points_service.spend_on_purchase(
                        self.store, customer, want, sale.discounted_total,
                        source="بيع", source_id=sale.pk, sale=sale,
                    )
                    if spent:
                        sale.beans_spent = spent
                        sale.discounted_total = max(
                            Decimal("0.00"), sale.discounted_total - points_service.value_of(spent)
                        )
                points_service.award_for_purchase(
                    self.store, customer, sale.discounted_total,
                    source="بيع", source_id=sale.pk, sale=sale,
                )
                models.LoyaltyProfile.objects.for_pharmacy(self.store).filter(
                    customer=customer
                ).update(last_visit_at=when)
            sale.save(update_fields=["total", "discounted_total", "beans_spent"])
        return sale

    def _return(self, sale, when, staff):
        from apps.store.cafe_api import record_return

        line = self.rng.choice(list(sale.items.all()))
        reason = self.rng.choices([r for r, _ in RETURN_REASONS], weights=[w for _, w in RETURN_REASONS])[0]
        remake = reason in ("spilled", "late") and self.rng.random() < 0.7
        later = when + timedelta(minutes=self.rng.randint(2, 15))
        if later >= timezone.now():
            return 0
        with frozen(later):
            row = record_return(sale, {
                "sale_item": line.pk, "quantity": Decimal("1"),
                "refund": "none" if remake else "full", "reason": reason,
                "note": "أُعيد تحضيره" if remake else "",
            }, self.rng.choice(staff) if staff else None)
        self.mark.add(row)
        return 1

    # ── inventory ────────────────────────────────────────────────────────
    def _inventory(self, start, today, staff):
        from apps.store.cafe_api import _apply_move

        tz = timezone.get_current_timezone()
        owner = next((u for u in staff if getattr(u, "role", "") == "owner"), staff[0] if staff else None)
        moves = 0
        for name, cat, unit, pqty, pcost, daily, reorder, life in MATERIALS:
            base_pack = models.InventoryItem.to_base(pqty, unit)
            at0 = timezone.make_aware(datetime.combine(start, time(12)), tz)
            with frozen(at0):
                item = models.InventoryItem.objects.create(
                    store=self.store, name=name, category=cat, purchase_qty=pqty,
                    purchase_unit=unit, purchase_cost=Decimal(pcost), reorder_level=Decimal(reorder),
                    supplier={"ألبان": "ألبان الجنيدي", "فواكه": "سوق الخضار", "قهوة": "محمصة النورس",
                              "سيرب": "تموين قلقيلية", "جاف": "تموين قلقيلية"}.get(cat, "مطبعة الأمل للتغليف"),
                )
            self.mark.add(item)
            level = Decimal(reorder) * 2
            with frozen(at0):
                _apply_move(item, "count", level, owner, reason="رصيد افتتاحي")
            moves += 1
            sim = level
            d = start
            while d <= today:
                use = Decimal(daily) * Decimal(str(self.rng.uniform(0.7, 1.3))) * Decimal(WEEKDAY[d.weekday()])
                sim = max(Decimal("0"), sim - use)
                noon = timezone.make_aware(datetime.combine(d, time(12, self.rng.randint(0, 50))), tz)
                if noon >= timezone.now():
                    break
                # A delivery when the shelf runs low.
                if sim < Decimal(reorder):
                    packs = max(1, int((Decimal(reorder) * 3 - sim) / base_pack) + 1)
                    # Prices drift a little, so unit cost moves between deliveries.
                    price = money(Decimal(pcost) * Decimal(str(self.rng.uniform(0.95, 1.08))))
                    with frozen(noon):
                        item.refresh_from_db()
                        item.purchase_qty = Decimal(pqty) * packs
                        item.purchase_unit = unit
                        item.purchase_cost = money(price * packs)
                        if life:
                            item.expiry_date = d + timedelta(days=life)
                        item.save()
                        _apply_move(item, "purchase", base_pack * packs, owner,
                                    total_cost=item.purchase_cost, note=f"{packs} × {pqty} {unit}")
                    sim += base_pack * packs
                    moves += 1
                # Milk goes off, a bag of bananas turns.
                if life and life <= 10 and self.rng.random() < 0.2:
                    lost = min(sim, (Decimal(daily) * Decimal(str(self.rng.uniform(0.2, 0.6)))).quantize(Decimal("1")))
                    if lost > 0:
                        with frozen(noon + timedelta(hours=1)):
                            item.refresh_from_db()
                            _apply_move(item, "waste", -lost, self.rng.choice(staff) if staff else None,
                                        reason=self.rng.choice(["انتهت الصلاحية", "تلف", "انسكب"]))
                        sim -= lost
                        moves += 1
                # Saturday count: the shelf, as it really is.
                if d.weekday() == 5:
                    with frozen(noon + timedelta(hours=2)):
                        item.refresh_from_db()
                        diff = sim.quantize(Decimal("1")) - Decimal(item.stock)
                        if diff:
                            _apply_move(item, "count", diff, owner, reason="جرد أسبوعي")
                            moves += 1
                d += timedelta(days=1)
            # Leave a couple of items low and one expiring, so the page has
            # something to warn about.
        low = models.InventoryItem.objects.for_pharmacy(self.store).filter(
            pk__in=[m.object_pk for m in self.mark.rows if m.model == "store.InventoryItem"]
        ).order_by("?")[:2]
        for it in low:
            target = (Decimal(it.reorder_level) * Decimal("0.6")).quantize(Decimal("1"))
            diff = target - Decimal(it.stock)
            if diff:
                _apply_move(it, "count", diff, owner, reason="جرد")
                moves += 1
        return moves

    # ── expenses ─────────────────────────────────────────────────────────
    def _expenses(self, start, today):
        finance.ensure_default_categories(self.store.pk)
        cats = {c.key: c for c in models.ExpenseCategory.objects.for_pharmacy(self.store) if c.key}
        first = start.replace(day=1)
        n = 0
        for key, name, amount in EXPENSES_RECURRING:
            if key not in cats:
                continue
            r = models.RecurringExpense.objects.create(
                store=self.store, category=cats[key], name=name, amount=amount, start_month=first
            )
            self.mark.add(r)
            n += 1
        for month in finance._months(first, today):
            done = month < today.replace(day=1)
            for key, lo, hi in EXPENSES_MONTHLY:
                if key not in cats or not done:
                    continue
                e = models.Expense.objects.create(
                    store=self.store, category=cats[key], amount=Decimal(self.rng.randint(lo, hi)),
                    period=month, paid_on=(month + timedelta(days=35)).replace(day=self.rng.randint(3, 12)),
                    note="فاتورة الشهر",
                )
                self.mark.add(e)
                n += 1
            for key, lo, hi, p in EXPENSES_SOMETIMES:
                if key not in cats or self.rng.random() > p:
                    continue
                e = models.Expense.objects.create(
                    store=self.store, category=cats[key], amount=Decimal(self.rng.randint(lo, hi)),
                    period=month, paid_on=min(today, month + timedelta(days=self.rng.randint(2, 25))),
                    note={"maintenance": "صيانة ماكينة القهوة", "marketing": "إعلان ممول",
                          "other": "مستلزمات تنظيف"}[key],
                )
                self.mark.add(e)
                n += 1
        return n

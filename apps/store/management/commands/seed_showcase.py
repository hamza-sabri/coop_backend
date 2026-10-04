"""Fill a shop with believable trade so every screen has something to show.

    python manage.py seed_showcase                    # this deployment's shop, last 90 days
    python manage.py seed_showcase --days 90 --sales 10000 --customers 100 --store coop
    python manage.py seed_showcase --staff-password demo12345

and when the show is over:

    python manage.py purge_showcase --yes

What it makes, all of it registered in DemoMark so the purge removes exactly
this and nothing the shop entered itself:

  * 100 customers with profile photos, each with a favourite
    drink and a usual time of day; points earned, spent and reversed
  * ~10,000 sales over three months shaped like a café day — quiet at opening, a rush
    16:00–18:00 and another 21:00–23:00, busier Thursday/Friday nights
  * returns with reasons, including remakes
  * raw materials (cups, milk, beans, fruit …) with purchases, waste and
    weekly counts
  * three months of expenses: rent, salaries and internet as recurring
    costs; electricity, water, maintenance and marketing as monthly bills
  * two shifts (صباحي 13:00–19:00, مسائي 19:00–02:00) and four demo staff
    with pictures; each sale is rung by someone on that shift
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
from django.db.models import Sum
from django.utils import timezone

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.store import finance, models, recipes

#: Placeholder portraits (randomuser.me — free for demos), 0–99 per gender.
PHOTO_URL = "https://randomuser.me/api/portraits/{who}/{n}.jpg"
from apps.store import points as points_service
from apps.store.costing import unit_cost_for

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

#: key, name, amount, who is paid. Salaries are per employee (see _expenses).
EXPENSES_RECURRING = [("rent", "إيجار المحل", Decimal("3000"), "صاحب العقار"),
                      ("internet", "إنترنت", Decimal("150"), "شركة الاتصالات")]
SALARIES = [Decimal("1900"), Decimal("1800"), Decimal("2000"), Decimal("1850")]
PAYEES = {"electricity": "شركة الكهرباء", "water": "البلدية", "maintenance": "فني الماكينات",
          "marketing": "إعلان انستغرام", "other": "محل التنظيف"}
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
        parser.add_argument("--store", default=None, help="store slug (default: this deployment's shop)")
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--customers", type=int, default=100)
        parser.add_argument("--sales", type=int, default=10000, help="roughly how many sales in the window")
        parser.add_argument("--no-pictures", action="store_true", help="skip profile photos")
        parser.add_argument("--media-base", default="",
                            help="prefix for local /media URLs when storage is not B2 (e.g. http://127.0.0.1:8000)")
        parser.add_argument("--seed", type=int, default=7, help="random seed (same data each run)")
        parser.add_argument("--staff-password", default="",
                            help="password for the two demo staff (default: they cannot log in)")

    def handle(self, *args, **o):
        from apps.store.management.commands._store import resolve_store

        store = resolve_store(o["store"])
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
        self.pictures = not o["no_pictures"]
        self.photo_n = {"women": 0, "men": 0}
        self.can_download = True
        self.media_base = (o["media_base"] or "").rstrip("/")
        self.target_sales = max(100, int(o["sales"]))
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
            # Spread over the window so "new customers this month" is believable.
            customers = self._customers(int(o["customers"]), start)
            self.owner = next((u for u in staff if getattr(u, "role", "") == "owner"), staff[0] if staff else None)
            self.favourites = self._tastes(customers, products)
            self.items = self._materials(start)
            n_recipes = self._recipes(products)
            self.mark.flush()
        self.stdout.write(f"  recipes: {n_recipes} lines across {len(products)} drinks")
        n_sales, n_returns = self._sales(products, customers, staff, start, today)
        for name, item in self.items.items():
            pace = self.sim[name].get("ema")
            if pace:
                models.InventoryItem.objects.for_pharmacy(store).filter(pk=item.pk).update(
                    reorder_level=(pace * 3).quantize(Decimal("1"))
                )
        with transaction.atomic():
            n_exp = self._expenses(start, today)
            self.mark.flush()
        n_moves = models.StockMove.objects.for_pharmacy(store).filter(
            item_id__in=[i.pk for i in self.items.values()]
        ).count()

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

    #: username, name, gender, shift — two on each shift.
    STAFF = [("demo-layan", "ليان", "female", "صباحي"), ("demo-rahaf", "رهف", "female", "صباحي"),
             ("demo-anas", "أنس", "male", "مسائي"), ("demo-karim", "كريم", "male", "مسائي")]

    def _staff(self, password):
        User = get_user_model()
        out = list(User.objects.filter(store=self.store, is_active=True))
        self.on_shift = {"صباحي": [], "مسائي": []}
        for uname, first, gender, shift in self.STAFF:
            u = User.objects.filter(username=uname).first()
            if u is None:
                u = User(username=uname, first_name=first, display_name=first,
                         store=self.store, role="employee")
                if password:
                    u.set_password(password)
                else:
                    u.set_unusable_password()
                u.save()
                self.mark.add(u)
                if self.pictures:
                    # Staff take the top of the range so no customer shares a face.
                    who = "women" if gender == "female" else "men"
                    n = 99 - [x[0] for x in self.STAFF if (x[2] == "female") == (gender == "female")].index(uname)
                    data = self._download(PHOTO_URL.format(who=who, n=n))
                    if data:
                        u.avatar.save(f"{uname}.jpg", ContentFile(data), save=True)
                        self.mark.add(u, model="file:storage", restore={"key": u.avatar.name})
                    else:
                        u.profile_image_url = PHOTO_URL.format(who=who, n=n)
                        u.save(update_fields=["profile_image_url"])
                out.append(u)
            if u.store_id == self.store.pk:
                self.on_shift[shift].append(u)
        return out

    def _download(self, url):
        """The photo's bytes, or None. One failure and we stop trying — a
        server with no way out should not wait on a hundred timeouts."""
        if not self.can_download:
            return None
        try:
            import requests

            r = requests.get(url, timeout=8)
            if r.ok and r.headers.get("content-type", "").startswith("image/"):
                return r.content
        except Exception:
            pass
        self.can_download = False
        self.stdout.write("  photos: cannot download here — linking them instead")
        return None

    def _picture(self, female, owner):
        """A real photo for a customer: copied into this shop's storage when
        the server can reach it, else linked. A different face each time
        (100 per gender)."""
        from apps.core.uploads import B2_SCHEME

        who = "women" if female else "men"
        n = self.photo_n[who] % 90  # 90–99 are the staff's
        self.photo_n[who] += 1
        url = PHOTO_URL.format(who=who, n=n)
        data = self._download(url)
        if data is None:
            return url
        key = default_storage.save(f"avatars/showcase/{who}-{n:02d}.jpg", ContentFile(data))
        self.mark.add(owner, model="file:storage", restore={"key": key})
        if getattr(settings, "STORAGE_ENABLED", False):
            return f"{B2_SCHEME}{key}"
        local = default_storage.url(key)
        return f"{self.media_base}{local}" if local.startswith("/") else local

    def _tastes(self, customers, products):
        """Each customer has a drink they keep coming back for and a time of
        day they usually come — so their profile has a story to tell."""
        drinks = [p for p in products if Decimal(p.price or 0) <= 50] or products
        self.joined = {}
        self.band = {}
        out = {}
        for c in customers:
            out[c.pk] = self.rng.choice(drinks)
            self.band[c.pk] = "am" if self.rng.random() < 0.4 else "pm"
        return out

    def _customers(self, n, start):
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
            span = max(1, (finance.today() - start).days - 3)
            # Most arrive early (the loyal core); the rest trickle in.
            joined = timezone.make_aware(
                datetime.combine(start + timedelta(days=int(self.rng.random() ** 2 * span)), time(14)),
                timezone.get_current_timezone(),
            )
            joined = min(joined, timezone.now())
            with frozen(joined):
                c = models.Customer.objects.create(
                    store=self.store, name=name, phone=phone,
                    gender="female" if female else "male",
                )
            self.mark.add(c)
            if self.pictures:
                c.avatar = self._picture(female, c)
                models.Customer.objects.for_pharmacy(self.store).filter(pk=c.pk).update(avatar=c.avatar)
            c._joined = joined
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
        self.pools = {
            band: [c for c in regulars if self.band[c.pk] == band] or regulars for band in ("am", "pm")
        }
        sold = 0
        d = start
        while d <= today:
            per_day = self.target_sales / ((today - start).days + 1) / 0.971
            base = per_day * self.rng.uniform(0.88, 1.12) * WEEKDAY[d.weekday()]
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
            self._day_inventory(d, staff)
            if len(sale_marks) > 500:
                models.DemoMark.objects.bulk_create(sale_marks, ignore_conflicts=True)
                sale_marks = []
            d += timedelta(days=1)
        models.DemoMark.objects.bulk_create(sale_marks, ignore_conflicts=True)
        self.mark.flush()
        return sold, n_returns

    def _one_sale(self, products, customers, regulars, staff, when):
        lines = self._ticket_lines(products)
        local = timezone.localtime(when)
        band = "am" if 13 <= local.hour < 19 else "pm"
        r = self.rng.random()
        customer = None
        for _ in range(4):
            if r < 0.2:
                # A regular, usually at their usual time.
                pool = self.pools[band] if self.rng.random() < 0.8 else self.pools["pm" if band == "am" else "am"]
                cand = self.rng.choice(pool)
            elif r < 0.34:
                cand = self.rng.choice(customers)
            else:
                break
            if cand._joined <= when:  # nobody buys before they joined
                customer = cand
                break
        if customer is not None and self.rng.random() < 0.55:
            fav = self.favourites[customer.pk]
            variants = [v for v in fav.variants.all() if v.is_active]
            lines[0] = (fav, self.rng.choice(variants) if variants and self.rng.random() < 0.8 else None, 1)
        crew = self.on_shift.get("صباحي" if band == "am" else "مسائي") or []
        cashier = (self.owner if (self.rng.random() < 0.08 or not crew) and self.owner else
                   self.rng.choice(crew) if crew else (self.rng.choice(staff) if staff else None))
        with frozen(when), transaction.atomic():
            sale = None
            for _ in range(5):
                try:
                    with transaction.atomic():
                        sale = models.Sale.objects.create(
                            store=self.store, customer=customer, payment_method="cash",
                            receipt_code=models.Sale.new_receipt_code(when),
                            created_by=cashier,
                        )
                    break
                except IntegrityError:
                    sale = None
            if sale is None:
                return None
            items = []
            for p, v, qty in lines:
                price = Decimal((v.price if v and v.price else p.price) or 0)
                cost = self._unit_cost(p, v, when)
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
            # The same path a till sale takes: the recipe comes off the shelf.
            recipes.consume_sale(sale, user=sale.created_by)
        return sale

    def _unit_cost(self, p, v, when):
        """costing.unit_cost_for, cached per drink per day (prices move only
        when a delivery is booked, which happens between days here)."""
        key = (when.date(), p.pk, getattr(v, "pk", None))
        cache = self.__dict__.setdefault("_cost_cache", {})
        if key not in cache:
            cache[key] = unit_cost_for(p, v)
        return cache[key]

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
    def _materials(self, start):
        """Raw materials with an opening balance on the first morning."""
        tz = timezone.get_current_timezone()
        at0 = timezone.make_aware(datetime.combine(start, time(10)), tz)
        out = {}
        self.sim = {}
        for name, cat, unit, pqty, pcost, daily, reorder, life in MATERIALS:
            with frozen(at0):
                item = models.InventoryItem.objects.create(
                    store=self.store, name=name, category=cat, purchase_qty=pqty,
                    purchase_unit=unit, purchase_cost=Decimal(pcost), reorder_level=Decimal(reorder),
                    expiry_date=(start + timedelta(days=life)) if life else None,
                    supplier={"ألبان": "ألبان الجنيدي", "فواكه": "سوق الخضار", "قهوة": "محمصة النورس",
                              "سيرب": "تموين قلقيلية", "جاف": "تموين قلقيلية"}.get(cat, "مطبعة الأمل للتغليف"),
                )
                self.mark.add(item)
                from apps.store.cafe_api import _apply_move

                _apply_move(item, models.StockMove.Kind.ADJUST, Decimal(reorder) * 3, self.owner,
                            reason="رصيد افتتاحي")
            out[name] = item
            self.sim[name] = {"daily": Decimal(daily), "unit": unit, "pqty": pqty, "pcost": pcost, "life": life}
        return out

    def _recipes(self, products) -> int:
        """A believable recipe for every drink on the menu, by what its name
        says it is. Real recipes replace these; the purge removes them."""
        it = self.items
        n = 0
        for p in products:
            name = p.name
            cat = p.category.name if p.category_id else ""
            cold = any(w in name for w in ("آيس", "مثلج", "بارد")) or "بارد" in cat
            lines = []
            if any(w in name for w in ("سموذي", "عصير", "جوافة", "برتقال", "موز")) or "سموذي" in cat or "عصائر" in cat:
                fruit = ("فراولة مجمدة" if "بيري" in name or "فراولة" in name else
                         "جوافة" if "جوافة" in name else "برتقال للعصير" if "برتقال" in name else
                         "موز" if "موز" in name else "فراولة مجمدة")
                lines = [("كاسات بلاستيك للبارد", "1"), ("مصاصات", "1"), (fruit, "150"), ("سكر", "15")]
            elif "بروتين" in name or "بروتين" in cat:
                lines = [("كاسات بلاستيك للبارد", "1"), ("مصاصات", "1"), ("بودرة بروتين", "30"),
                         ("حليب كامل الدسم", "250")]
                if "شوكولات" in name:
                    lines.append(("كاكاو", "15"))
                if "بيري" in name:
                    lines.append(("فراولة مجمدة", "80"))
            elif any(w in name for w in ("لاتيه", "قهوة", "كابتشينو", "موكا", "اسبريسو", "إسبريسو", "أمريكانو", "كراميل", "فلات")) or "قهوة" in cat:
                lines = [("كاسات بلاستيك للبارد" if cold else "أكواب ورق 12oz", "1"), ("أغطية أكواب", "1"),
                         ("بن إسبريسو", "18")]
                if cold:
                    lines.append(("مصاصات", "1"))
                if not any(w in name for w in ("اسبريسو", "إسبريسو", "أمريكانو")):
                    lines.append(("حليب كامل الدسم", "200"))
                if "كراميل" in name:
                    lines.append(("سيرب كراميل", "20"))
                if "بندق" in name:
                    lines.append(("سيرب بندق", "20"))
                if "موكا" in name or "شوكولات" in name:
                    lines.append(("كاكاو", "15"))
                if "سبانش" in name:
                    lines.append(("كريمة خفق", "30"))
            elif any(w in name for w in ("شاي", "تي")):
                lines = [("كاسات بلاستيك للبارد" if cold else "أكواب ورق 12oz", "1"), ("مصاصات", "1"), ("سكر", "20")]
            elif "فطور" in cat or "حلويات" in cat or any(w in name for w in ("كيك", "كوكيز", "توست", "بوكس")):
                lines = [("محارم", "2")]
            else:
                lines = [("أكواب ورق 12oz", "1"), ("أغطية أكواب", "1")]
            for i, (iname, q) in enumerate(lines):
                if iname not in it:
                    continue
                r = models.RecipeLine.objects.create(
                    store=self.store, product=p, item=it[iname], quantity=Decimal(q),
                    display_unit=it[iname].unit, position=i,
                )
                self.mark.add(r)
                n += 1
        return n

    def _day_inventory(self, d, staff):
        """After a day's trade: deliveries for what ran low, the odd spoilage,
        and on Saturday a count that finds the small losses every shelf has."""
        from apps.store.cafe_api import _apply_move

        tz = timezone.get_current_timezone()
        noon = timezone.make_aware(datetime.combine(d + timedelta(days=1), time(11, self.rng.randint(0, 50))), tz)
        if noon >= timezone.now():
            return
        in_recipe = set(
            models.RecipeLine.objects.for_pharmacy(self.store).values_list("item_id", flat=True).distinct()
        )
        # What each ingredient actually went through today, smoothed — the
        # café orders by its own pace, like a real owner would.
        lo, hi = finance.bounds(d, d)
        used_today = dict(
            models.StockMove.objects.for_pharmacy(self.store)
            .filter(kind__in=("sale", "remake"), created_at__gte=lo, created_at__lt=hi)
            .values("item_id").annotate(q=Sum("quantity")).values_list("item_id", "q")
        )
        for name, item in self.items.items():
            sim = self.sim[name]
            item.refresh_from_db()
            if item.pk in in_recipe:
                use = -Decimal(used_today.get(item.pk) or 0)
                sim["ema"] = use if "ema" not in sim else sim["ema"] * Decimal("0.7") + use * Decimal("0.3")
            # Things no recipe covers (napkins, sugar for the counter) are
            # simply used; that use shows up as the weekly count.
            if item.pk not in in_recipe:
                sim.setdefault("shelf", Decimal(item.stock))
                use = sim["daily"] * Decimal(str(self.rng.uniform(0.7, 1.2)))
                sim["shelf"] = max(Decimal("0"), sim["shelf"] - use)
            # Spoilage on the perishables.
            if sim["life"] and sim["life"] <= 10 and self.rng.random() < 0.15 and item.stock > 0:
                lost = min(Decimal(item.stock), (sim["daily"] * Decimal(str(self.rng.uniform(0.05, 0.2)))).quantize(Decimal("1")))
                if lost > 0:
                    with frozen(noon - timedelta(hours=1)):
                        _apply_move(item, "waste", -lost, self.rng.choice(staff) if staff else None,
                                    reason=self.rng.choice(["انتهت الصلاحية", "تلف", "انسكب"]))
                    item.refresh_from_db()
            # Saturday count.
            if d.weekday() == 5:
                with frozen(noon - timedelta(minutes=30)):
                    if item.pk in in_recipe:
                        missing = (Decimal(max(item.stock, 0)) * Decimal(str(self.rng.uniform(0, 0.005)))).quantize(Decimal("1"))
                        if missing:
                            _apply_move(item, "count", -missing, self.owner, reason="جرد أسبوعي")
                    else:
                        diff = sim["shelf"].quantize(Decimal("1")) - Decimal(item.stock)
                        if diff:
                            _apply_move(item, "count", diff, self.owner, reason="جرد أسبوعي")
                item.refresh_from_db()
            # Delivery when below ~3 days of use (or the item's own level for
            # things no recipe covers), topped up to ~9 days.
            stock = Decimal(item.stock) if item.pk in in_recipe else sim.get("shelf", Decimal(item.stock))
            pace = sim.get("ema") or Decimal("0")
            level = max(pace * 3, Decimal(item.reorder_level) if not pace else Decimal("0"))
            # Not every delivery lands on time: some days the owner is a day late.
            if stock < level and self.rng.random() < 0.85:
                base_pack = models.InventoryItem.to_base(sim["pqty"], sim["unit"])
                target = pace * Decimal(str(self.rng.uniform(8, 11))) if pace else level * 3
                packs = max(1, int((target - stock) / base_pack) + 1)
                price = money(Decimal(sim["pcost"]) * Decimal(str(self.rng.uniform(0.95, 1.08))))
                with frozen(noon):
                    item.refresh_from_db()
                    item.purchase_qty = Decimal(sim["pqty"]) * packs
                    item.purchase_unit = sim["unit"]
                    item.purchase_cost = money(price * packs)
                    if sim["life"]:
                        item.expiry_date = d + timedelta(days=1 + sim["life"])
                    item.save()
                    _apply_move(item, "purchase", base_pack * packs, self.owner,
                                total_cost=item.purchase_cost, note=f"{packs} × {sim['pqty']} {sim['unit']}")
                if item.pk not in in_recipe:
                    sim["shelf"] = stock + base_pack * packs
                self.__dict__["_cost_cache"] = {}

    # ── expenses ─────────────────────────────────────────────────────────
    def _expenses(self, start, today):
        finance.ensure_default_categories(self.store.pk)
        cats = {c.key: c for c in models.ExpenseCategory.objects.for_pharmacy(self.store) if c.key}
        first = start.replace(day=1)
        n = 0
        for key, name, amount, payee in EXPENSES_RECURRING:
            if key not in cats:
                continue
            r = models.RecurringExpense.objects.create(
                store=self.store, category=cats[key], name=name, amount=amount, payee=payee, start_month=first
            )
            self.mark.add(r)
            n += 1
        # A monthly salary for each demo employee, by name.
        crew = [u for team in self.on_shift.values() for u in team]
        for u, amount in zip(crew, SALARIES):
            if "salaries" not in cats:
                break
            r = models.RecurringExpense.objects.create(
                store=self.store, category=cats["salaries"], name=f"راتب {u.staff_name}", amount=amount,
                staff=u, start_month=first,
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
                    note="فاتورة الشهر", payee=PAYEES.get(key, ""),
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
                    payee=PAYEES.get(key, ""),
                )
                self.mark.add(e)
                n += 1
        return n

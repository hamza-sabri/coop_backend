"""Fill كوب with believable trade so the admin has something to show.

    python manage.py seed_koup_demo                # top up
    python manage.py seed_koup_demo --reset        # wipe كوب's demo rows first
    python manage.py seed_koup_demo --customers 60 --sales 400 --days 90

Everything is scoped to the كوب tenant, so this can never touch another
store's data. Sales are spread across the last N days with a realistic shape:
a morning peak, an evening peak, quiet Mondays, busy weekends — otherwise
every chart in the reports page is a flat line and tells you nothing.

Beans are written through the ledger, never straight onto the balance, so the
demo exercises the same path production will.
"""
import random
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.store.models import (
    BeanLedger, Customer, LoyaltyProfile, Product, Sale, SaleItem, Store,
)

SLUG = "koup"

FIRST_M = ["أحمد", "محمد", "عمر", "يوسف", "خالد", "سامر", "باسل", "زياد", "مراد",
           "طارق", "أنس", "رامي", "وسيم", "نادر", "هاني", "إياد"]
FIRST_F = ["سارة", "ليان", "دانا", "رند", "مريم", "نور", "هبة", "رنا", "لمى",
           "جنى", "ياسمين", "سلمى", "تالا", "بيان", "شهد", "زينة"]
LAST = ["عبد الله", "أبو زيد", "الحاج", "دعمس", "زيد", "صبري", "عودة", "نزال",
        "شواهنة", "مرعي", "الخطيب", "قاسم", "حمد", "طه", "ناصر", "عمران"]

# The photos we generated, matched to the menu by name.
IMAGES = {
    "آيس لاتيه كراميل": "caramel", "آيس لاتيه بندق": "hazelnut",
    "سبانش لاتيه": "spanish", "كراميل بدون سكر": "sugarfree",
    "مشروب الجوافة": "guava", "سموذي بيري": "berry",
    "بروتين شيك شوكولاتة": "protein-choc", "بروتين شيك بيري": "protein-berry",
    "فرنش توست بالقرفة": "frenchtoast", "بوكس كوب": "koupbox",
    "تشيز كيك": "cheesecake", "كوكيز كوب": "cookie",
}


def busyness(when):
    """How likely a sale is at this moment. Cafés are not uniform."""
    h, wd = when.hour, when.weekday()
    peak = 0.25
    if 7 <= h <= 10:   peak = 1.00        # the morning run
    elif 11 <= h <= 14: peak = 0.55
    elif 15 <= h <= 18: peak = 0.75
    elif 19 <= h <= 23: peak = 0.95       # the evening sit-down
    day = {0: .70, 1: .75, 2: .80, 3: .95, 4: 1.00, 5: 1.00, 6: .85}[wd]
    return peak * day


class Command(BaseCommand):
    help = "Seed كوب with customers, loyalty history and ~90 days of sales."

    def add_arguments(self, p):
        p.add_argument("--reset", action="store_true")
        p.add_argument("--force", action="store_true",
                       help="Seed again even if there is already trade.")
        p.add_argument("--customers", type=int, default=48)
        p.add_argument("--sales", type=int, default=320)
        p.add_argument("--days", type=int, default=90)
        p.add_argument("--seed", type=int, default=11)

    @transaction.atomic
    def handle(self, *a, **o):
        random.seed(o["seed"])
        now = timezone.now()
        try:
            store = Store.objects.get(slug=SLUG)
        except Store.DoesNotExist:
            raise CommandError("Run `python manage.py seed_koup` first.")
        pid = store.pk

        products = list(Product.objects.for_pharmacy(pid))
        if not products:
            raise CommandError("No products — run `python manage.py seed_koup` first.")

        # dev.sh runs this on every start; piling a second 90 days on top of the
        # first would make every report quietly wrong.
        if not o["reset"] and not o["force"] and Sale.objects.for_pharmacy(pid).exists():
            self.stdout.write("  demo data already present — skipping "
                              "(--reset to rebuild, --force to add more)")
            return

        if o["reset"]:
            BeanLedger.objects.for_pharmacy(pid).delete()
            LoyaltyProfile.objects.for_pharmacy(pid).delete()
            SaleItem.objects.for_pharmacy(pid).delete()
            Sale.objects.for_pharmacy(pid).delete()
            Customer.objects.for_pharmacy(pid).delete()
            self.stdout.write(self.style.WARNING("  reset: كوب demo rows wiped"))

        # ── product photos ─────────────────────────────────────────────────
        shot = 0
        for p in products:
            slug = IMAGES.get(p.name)
            if slug and p.image != f"/koup/menu/{slug}.webp":
                p.image = f"/koup/menu/{slug}.webp"
                p.save(update_fields=["image"])
                shot += 1
        self.stdout.write(f"  photos: {shot} products")

        # ── customers + where they stand in the programme ──────────────────
        staff = list(get_user_model().objects.filter(store=store)) or [None]
        customers = []
        for i in range(o["customers"]):
            female = random.random() < .55
            name = f"{random.choice(FIRST_F if female else FIRST_M)} {random.choice(LAST)}"
            c = Customer.objects.create(
                store=store, name=name,
                phone=f"059{random.randint(1000000, 9999999)}" if random.random() < .82 else None,
                gender="female" if female else "male",
                client_uuid=str(uuid.uuid4()),
            )
            # A real base is mostly casual with a small hard core.
            r = random.random()
            tier = "triple" if r > .93 else "double" if r > .70 else "single"
            LoyaltyProfile.objects.create(
                store=store, customer=c, tier=tier, beans=0,
                visits_this_month=random.randint(10, 24) if tier != "single" else random.randint(0, 8),
                streak_weeks=random.randint(3, 14) if tier != "single" else random.randint(0, 3),
                last_visit_at=now - timedelta(days=random.randint(0, 20)),
                tier_until=(now + timedelta(days=random.randint(20, 90))).date() if tier != "single" else None,
            )
            customers.append(c)
        self.stdout.write(f"  customers: {len(customers)}")

        # ── sales, shaped like a week in a café ────────────────────────────
        def bean_row(cust, delta, reason, sale, when, note=""):
            prof = cust.loyalty
            prof.beans = max(0, prof.beans + delta)
            prof.save(update_fields=["beans"])
            row = BeanLedger.objects.create(
                store=store, customer=cust, delta=delta, reason=reason, sale=sale,
                balance_after=prof.beans, note=note,
                expires_at=(when + timedelta(days=180)).date() if delta > 0 else None,
                idempotency_key=uuid.uuid4().hex,
            )
            # auto_now_add ignores anything passed to create(), so the real
            # timestamp is written after. Scoped, like every other read.
            BeanLedger.objects.for_pharmacy(pid).filter(pk=row.pk).update(created_at=when)

        for c in customers:                                   # endowed progress
            bean_row(c, 5, "signup", None, now - timedelta(days=random.randint(30, 120)),
                     "٥ حبّات هدية التسجيل")

        made = 0
        for _ in range(o["sales"] * 3):
            if made >= o["sales"]:
                break
            when = now - timedelta(days=random.uniform(0, o["days"]),
                                   hours=random.uniform(0, 24))
            if random.random() > busyness(when):
                continue
            cust = random.choice(customers) if random.random() < .72 else None
            lines = random.choices([1, 2, 3, 4], weights=[46, 32, 16, 6])[0]
            picked = random.sample(products, min(lines, len(products)))
            sale = Sale.objects.create(
                store=store, customer=cust, created_by=random.choice(staff),
                payment_method="cash" if random.random() < .88 else "card",
                total=Decimal(0), discounted_total=Decimal(0),
                client_uuid=str(uuid.uuid4()),
            )
            total = Decimal(0)
            for p in picked:
                q = Decimal(random.choices([1, 2, 3], weights=[78, 18, 4])[0])
                line = (p.price * q).quantize(Decimal("0.01"))
                SaleItem.objects.create(
                    sale=sale, product=p, medication_name=p.name,
                    category=p.category.name if p.category_id else "",
                    unit_price=p.price, original_unit_price=p.price,
                    quantity=q, line_total=line,
                )
                total += line
            Sale.objects.for_pharmacy(pid).filter(pk=sale.pk).update(
                total=total, discounted_total=total, created_at=when)
            if cust:                                   # 1 حبّة per ₪5, tiered
                mult = cust.loyalty.multiplier
                earned = int((total / Decimal(5)) * mult)
                if earned:
                    bean_row(cust, earned, "earn", sale, when, f"طلب بقيمة ₪{total}")
            made += 1

        # a handful of redemptions, so the ledger is not one-directional
        spenders = [c for c in customers if c.loyalty.beans > 80]
        for c in random.sample(spenders, min(14, len(spenders))):
            p = random.choice(products)
            beans = int(p.price * Decimal("3.33"))
            bean_row(c, -beans, "redeem", None,
                     now - timedelta(days=random.randint(1, 40)), f"{p.name} — مجاناً")

        self.stdout.write(f"  sales: {made} across {o['days']} days")
        self.stdout.write(f"  ledger: {BeanLedger.objects.for_pharmacy(pid).count()} rows")
        total_beans = sum(LoyaltyProfile.objects.for_pharmacy(pid).values_list("beans", flat=True))
        self.stdout.write(self.style.SUCCESS(
            f"\n  كوب has trade now.\n"
            f"  outstanding beans: {total_beans}  "
            f"(≈ ₪{total_beans / 3.33:.0f} of liability)\n"))

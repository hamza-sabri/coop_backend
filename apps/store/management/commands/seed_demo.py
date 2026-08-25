"""Seed a self-contained, tenant-safe DEMO store for public showcasing.

Everything lives under ONE tenant — the `demo` store — so the public demo
can be wiped and reseeded nightly without ever touching a real customer's data.

What it builds:
  * a `demo` Store (slug="demo") + a `demo/demo` login (all modules)
  * a few staff accounts (the "recorded by" of debts/sales), all in the tenant
  * a realistic Arabic product catalogue (categories, manufacturers, stock)
  * male & female customers with Arabic names, some without a phone
  * debts — itemised (real meds, price snapshotted) and plain-amount, paid/unpaid
  * POS sales spread across ~90 days so the dashboard & charts have shape

Safety: `--reset` deletes ONLY the demo tenant's rows (never global). This
command never deletes or edits any other store's data.

Usage:
    python manage.py seed_demo --reset          # wipe demo tenant + reseed (nightly)
    python manage.py seed_demo                   # top-up without wiping
    python manage.py seed_demo --customers 120 --debts 300 --sales 500 --seed 7
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.store.models import (
    Category,
    Customer,
    Debt,
    DebtItem,
    Manufacturer,
    Product,
    Store,
    Sale,
    SaleItem,
)

TWO = Decimal("0.01")

DEMO_SLUG = "demo"
DEMO_NAME = "صيدلية فارما التجريبية"
DEMO_LOGIN = ("demo", "demo")  # username, password — public showcase creds

STAFF = [
    ("demo_sami", "سامي", "الصيدلي"),
    ("demo_rana", "رنا", "الصيدلانية"),
    ("demo_ahmad", "أحمد", "المحاسب"),
    ("demo_heba", "هبة", "المساعدة"),
]

# ---- Realistic Arabic name pools ------------------------------------------
MALE_FIRST = [
    "محمد", "أحمد", "محمود", "خالد", "عمر", "علي", "حسن", "حسين", "يوسف",
    "إبراهيم", "عبدالله", "عبدالرحمن", "مصطفى", "سامي", "وليد", "ماجد", "طارق",
    "رامي", "زياد", "فادي", "ناصر", "سليم", "كريم", "باسل", "عماد", "نبيل",
    "جمال", "هاني", "أنس", "بلال", "مالك", "غسان", "رائد", "سائد", "أيمن",
]
FEMALE_FIRST = [
    "فاطمة", "عائشة", "مريم", "خديجة", "زينب", "سارة", "هدى", "ليلى", "نور",
    "رنا", "دعاء", "آية", "إسراء", "أسماء", "رغد", "سلمى", "ياسمين", "لينا",
    "دانا", "ميس", "جنى", "رهف", "شهد", "ربى", "هبة", "منى", "سناء", "أمل",
]
FAMILY = [
    "الأحمد", "الخطيب", "عبدالله", "حمدان", "أبو دية", "النجار", "العلي", "صالح",
    "درويش", "الحاج", "قاسم", "شاهين", "بركات", "عوض", "سلامة", "خليل", "جابر",
    "مرعي", "صبري", "ياسين", "دراغمة", "عمرو", "نمر", "الشريف", "أبو سالم",
]
STATUSES = ["منتظم", "جديد", "موثوق", "متأخر بالدفع", "دائم", "VIP", "زبون جملة"]
STATUS_WEIGHTS = [30, 22, 14, 10, 12, 5, 4]
CUSTOMER_NOTES = [
    "زبون دائم منذ سنوات", "يفضل التواصل مساءً", "دفعات شهرية منتظمة",
    "لديه وصفة دائمة للضغط", "يشتري بالجملة أحياناً", "قريب من الصيدلية",
    "يسأل دائماً عن العروض", "لديه حساسية من البنسلين", "يفضل البدائل الأرخص",
]
DEBT_NOTES = [
    "سيسدد نهاية الشهر", "دواء مزمن", "طلب عائلي", "بالتقسيط",
    "دفعة أولى، الباقي لاحقاً", "وصفة طبية", "طلب طارئ",
]

# ---- Product catalogue: (name, barcode, price, cost, category, maker, stock)
CATEGORIES = [
    "مسكنات", "مضاد حيوي", "فيتامينات ومكملات", "جهاز تنفسي", "جهاز هضمي",
    "عناية بالبشرة", "مستلزمات أطفال", "حساسية", "أمراض مزمنة", "مطهرات",
]
MANUFACTURERS = [
    "GSK", "Hikma", "Pfizer", "Bayer", "Sanofi", "Novartis", "Julphar",
    "Dar Al Dawa", "Jerusalem Pharma", "Birzeit Pharma", "Reckitt", "Nestlé",
]
MEDS = [
    ("بنادول أقراص", "6001082000019", "12.00", "7.50", "مسكنات", "GSK", 120),
    ("أدول 500", "6281006000023", "8.00", "5.00", "مسكنات", "Julphar", 90),
    ("بروفين 400", "5000167000047", "15.00", "9.50", "مسكنات", "Reckitt", 60),
    ("فولتارين جل", "7613103000051", "22.00", "14.00", "مسكنات", "Novartis", 40),
    ("كتافلام 50", "7613103000068", "18.00", "11.00", "مسكنات", "Novartis", 35),
    ("بروفينال 400", "6281006000078", "14.00", "8.50", "مسكنات", "Julphar", 55),
    ("أوجمنتين 1g", "5099609000085", "45.00", "30.00", "مضاد حيوي", "GSK", 25),
    ("أموكسيل 500", "5099609000092", "28.00", "18.00", "مضاد حيوي", "GSK", 30),
    ("كلاسيد 500", "0300810000108", "52.00", "34.00", "مضاد حيوي", "Hikma", 18),
    ("زيثروماكس 500", "0300810000115", "48.00", "31.00", "مضاد حيوي", "Pfizer", 20),
    ("فيتامين سي 1000", "6001082000125", "16.00", "9.00", "فيتامينات ومكملات", "Hikma", 100),
    ("سنترم", "0300055000132", "65.00", "42.00", "فيتامينات ومكملات", "Pfizer", 45),
    ("أوميغا 3", "0768990000149", "55.00", "35.00", "فيتامينات ومكملات", "Dar Al Dawa", 50),
    ("كالسيوم د3", "6281006000153", "30.00", "19.00", "فيتامينات ومكملات", "Julphar", 60),
    ("حديد شراب", "6001082000163", "24.00", "15.00", "فيتامينات ومكملات", "Hikma", 40),
    ("فنتولين بخاخ", "5099609000177", "35.00", "22.00", "جهاز تنفسي", "GSK", 30),
    ("فليموسيل", "7613103000184", "20.00", "12.50", "جهاز تنفسي", "Sanofi", 45),
    ("بنادول كولد", "6001082000194", "14.00", "8.50", "جهاز تنفسي", "GSK", 70),
    ("نكسيوم 40", "0300810000207", "58.00", "38.00", "جهاز هضمي", "Hikma", 22),
    ("أوميبرازول 20", "6281006000214", "18.00", "10.50", "جهاز هضمي", "Julphar", 65),
    ("جافيسكون", "5000167000221", "26.00", "16.00", "جهاز هضمي", "Reckitt", 40),
    ("بوسكوبان", "0300055000238", "22.00", "13.50", "جهاز هضمي", "Sanofi", 35),
    ("موتيليوم", "0300810000245", "24.00", "15.00", "جهاز هضمي", "Novartis", 30),
    ("بيتادين مطهر", "7613103000252", "19.00", "11.00", "مطهرات", "Bayer", 55),
    ("ديتول سائل", "5000167000269", "23.00", "14.00", "مطهرات", "Reckitt", 48),
    ("بانثينول كريم", "4008500000276", "27.00", "17.00", "عناية بالبشرة", "Bayer", 38),
    ("بيبانثين", "4008500000283", "34.00", "22.00", "عناية بالبشرة", "Bayer", 30),
    ("سودو كريم", "5011025000290", "38.00", "24.00", "مستلزمات أطفال", "Reckitt", 26),
    ("جونسون شامبو أطفال", "3574660000306", "29.00", "18.00", "مستلزمات أطفال", "Nestlé", 33),
    ("سيريلاك قمح", "7613030000313", "32.00", "21.00", "مستلزمات أطفال", "Nestlé", 40),
    ("كلاريتين", "0300055000320", "26.00", "16.50", "حساسية", "Bayer", 44),
    ("تلفاست 180", "0300810000337", "30.00", "19.00", "حساسية", "Sanofi", 36),
    ("أوتريفين نقط", "7613103000344", "17.00", "10.00", "جهاز تنفسي", "Novartis", 50),
    ("ستربسلز", "5000167000351", "13.00", "7.50", "جهاز تنفسي", "Reckitt", 80),
    ("أسبرين 100", "4008500000368", "9.00", "5.00", "أمراض مزمنة", "Bayer", 90),
    ("كونكور 5", "4008500000375", "28.00", "18.00", "أمراض مزمنة", "Sanofi", 40),
    ("جلوكوفاج 850", "5099609000382", "20.00", "12.00", "أمراض مزمنة", "Novartis", 55),
    ("ليبيتور 20", "0300810000399", "60.00", "40.00", "أمراض مزمنة", "Pfizer", 24),
    ("بنادول إكسترا", "6001082000405", "15.00", "9.00", "مسكنات", "GSK", 85),
    ("زنك أقراص", "0768990000412", "18.00", "11.00", "فيتامينات ومكملات", "Dar Al Dawa", 60),
]


def _q(v):
    return Decimal(v).quantize(TWO)


class Command(BaseCommand):
    help = "Seed a self-contained, tenant-safe demo store (demo/demo)."

    def add_arguments(self, parser):
        parser.add_argument("--customers", type=int, default=120)
        parser.add_argument("--debts", type=int, default=300)
        parser.add_argument("--sales", type=int, default=500)
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Wipe ONLY the demo tenant's data first (safe nightly reset).",
        )
        parser.add_argument("--seed", type=int, default=None, help="RNG seed.")

    @transaction.atomic
    def handle(self, *args, **opts):
        if opts["seed"] is not None:
            random.seed(opts["seed"])
        now = timezone.now()
        User = get_user_model()

        # ---- Demo tenant ---------------------------------------------------
        store, _ = Store.objects.get_or_create(
            slug=DEMO_SLUG,
            defaults={"name": DEMO_NAME, "phone": "0599000000", "address": "قلقيلية"},
        )
        pid = store.pk

        if opts["reset"]:
            # Scoped to the demo tenant ONLY. Order respects FKs.
            SaleItem.objects.for_pharmacy(pid).delete()
            Sale.objects.for_pharmacy(pid).delete()
            DebtItem.objects.for_pharmacy(pid).delete()
            Debt.objects.for_pharmacy(pid).delete()
            Customer.objects.for_pharmacy(pid).delete()
            Product.objects.for_pharmacy(pid).delete()
            Category.objects.for_pharmacy(pid).delete()
            Manufacturer.objects.for_pharmacy(pid).delete()
            self.stdout.write(self.style.WARNING("Reset: demo tenant data wiped."))

        # ---- Login + staff (all inside the demo tenant) --------------------
        demo_user, created = User.objects.get_or_create(
            username=DEMO_LOGIN[0], defaults={"store": store, "is_staff": False}
        )
        demo_user.store = store
        demo_user.set_password(DEMO_LOGIN[1])
        demo_user.save()

        staff_users = [demo_user]
        for username, first, last in STAFF:
            u, _ = User.objects.get_or_create(
                username=username,
                defaults={"first_name": first, "last_name": last,
                          "is_staff": True, "store": store},
            )
            if u.store_id != pid:
                u.store = store
                u.save(update_fields=["store"])
            staff_users.append(u)

        # ---- Categories & manufacturers ------------------------------------
        cat_by_name = {
            name: Category.objects.get_or_create(store=store, name=name)[0]
            for name in CATEGORIES
        }
        man_by_name = {
            name: Manufacturer.objects.get_or_create(store=store, name=name)[0]
            for name in MANUFACTURERS
        }

        # ---- Medications ---------------------------------------------------
        existing_barcodes = set(
            Product.objects.for_pharmacy(pid).values_list("barcode", flat=True)
        )
        to_create = []
        for name, barcode, price, cost, cat, man, stock in MEDS:
            if barcode in existing_barcodes:
                continue
            to_create.append(
                Product(
                    store=store, name=name, barcode=barcode,
                    price=_q(price), cost=_q(cost), stock=stock,
                    category=cat_by_name.get(cat), manufacturer=man_by_name.get(man),
                )
            )
        if to_create:
            Product.objects.bulk_create(to_create, batch_size=200)
        med_pool = list(
            Product.objects.for_pharmacy(pid).filter(price__gt=0)
            .values_list("id", "name", "price", "category__name")
        )

        # ---- Customers -----------------------------------------------------
        used_phones = set(
            Customer.objects.for_pharmacy(pid)
            .exclude(phone__isnull=True).values_list("phone", flat=True)
        )

        def new_phone():
            while True:
                p = "05" + random.choice("6789") + "".join(random.choices("0123456789", k=7))
                if p not in used_phones:
                    used_phones.add(p)
                    return p

        customers = []
        for i in range(opts["customers"]):
            gender = "male" if random.random() < 0.55 else "female"
            first = random.choice(MALE_FIRST if gender == "male" else FEMALE_FIRST)
            customers.append(Customer(
                store=store,
                name=f"{first} {random.choice(FAMILY)}",
                phone=new_phone() if random.random() > 0.12 else None,
                gender=gender,
                status=(random.choices(STATUSES, weights=STATUS_WEIGHTS, k=1)[0]
                        if random.random() < 0.78 else ""),
                notes=random.choice(CUSTOMER_NOTES) if random.random() < 0.4 else "",
                avatar=(f"https://i.pravatar.cc/200?u=pharma{pid}-{i}"
                        if random.random() < 0.2 else ""),
            ))
        Customer.objects.bulk_create(customers, batch_size=200)
        customers = list(Customer.objects.for_pharmacy(pid))

        # ---- Debts (itemised + plain amount) -------------------------------
        has_meds = bool(med_pool)
        debtors = random.sample(customers, k=max(1, int(len(customers) * 0.70))) if customers else []
        debts, items_spec, debt_dates = [], [], []
        for _ in range(opts["debts"] if debtors else 0):
            cust = random.choice(debtors)
            created = now - timedelta(days=random.randint(0, 300), hours=random.randint(0, 23))
            items, total = [], Decimal("0.00")
            if has_meds and random.random() < 0.7:
                for mid, mname, mprice, _c in random.sample(med_pool, k=min(random.randint(1, 4), len(med_pool))):
                    qty = random.randint(1, 3)
                    unit = _q(mprice)
                    line = _q(unit * qty)
                    items.append((mid, mname, unit, qty, line))
                    total += line
                total = _q(total)
            else:
                total = _q(random.randint(2, 80) * 5)
            discounted = _q(total * Decimal(str(round(random.uniform(0.70, 0.95), 2)))) \
                if total > 0 and random.random() < 0.30 else total
            debts.append(Debt(
                store=store, customer=cust, created_by=random.choice(staff_users),
                total=total, discounted_total=discounted, is_paid=random.random() < 0.45,
                note=random.choice(DEBT_NOTES) if random.random() < 0.30 else "",
            ))
            items_spec.append(items)
            debt_dates.append(created)
        if debts:
            Debt.objects.bulk_create(debts, batch_size=200)
            all_items = [
                DebtItem(debt_id=d.pk, medication_id=mid, medication_name=mname,
                         unit_price=unit, quantity=qty, line_total=line)
                for d, spec in zip(debts, items_spec) for (mid, mname, unit, qty, line) in spec
            ]
            if all_items:
                DebtItem.objects.bulk_create(all_items, batch_size=500)
            for d, created in zip(debts, debt_dates):
                d.created_at = created
            Debt.objects.bulk_update(debts, ["created_at"], batch_size=200)

        # ---- Sales (POS history for dashboards/charts) ---------------------
        sales, sale_items_spec, sale_dates = [], [], []
        for _ in range(opts["sales"] if has_meds else 0):
            created = now - timedelta(days=random.randint(0, 90), hours=random.randint(8, 21),
                                      minutes=random.randint(0, 59))
            chosen = random.sample(med_pool, k=min(random.randint(1, 4), len(med_pool)))
            spec, total = [], Decimal("0.00")
            for mid, mname, mprice, cname in chosen:
                qty = random.randint(1, 3)
                unit = _q(mprice)
                line = _q(unit * qty)
                spec.append((mid, mname, cname or "", unit, qty, line))
                total += line
            total = _q(total)
            discounted = _q(total * Decimal("0.95")) if random.random() < 0.2 else total
            sales.append(Sale(
                store=store,
                customer=random.choice(customers) if customers and random.random() < 0.25 else None,
                created_by=random.choice(staff_users), payment_method="cash",
                total=total, discounted_total=discounted,
            ))
            sale_items_spec.append(spec)
            sale_dates.append(created)
        if sales:
            Sale.objects.bulk_create(sales, batch_size=200)
            all_sitems = [
                SaleItem(sale_id=s.pk, medication_id=mid, medication_name=mname,
                         category=cname, unit_price=unit, quantity=qty, line_total=line)
                for s, spec in zip(sales, sale_items_spec) for (mid, mname, cname, unit, qty, line) in spec
            ]
            if all_sitems:
                SaleItem.objects.bulk_create(all_sitems, batch_size=500)
            for s, created in zip(sales, sale_dates):
                s.created_at = created
            Sale.objects.bulk_update(sales, ["created_at"], batch_size=200)

        # ---- Summary -------------------------------------------------------
        self.stdout.write(self.style.SUCCESS(
            f"Demo tenant '{store.slug}' ready — login {DEMO_LOGIN[0]}/{DEMO_LOGIN[1]}. "
            f"{len(med_pool)} meds, {len(customers)} customers, "
            f"{len(debts)} debts, {len(sales)} sales."
        ))

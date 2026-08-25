"""Seed the كوب tenant: the store, a local admin login, and the real menu.

    python manage.py seed_koup                 # idempotent — safe to re-run
    python manage.py seed_koup --reset         # wipe كوب's catalogue, reseed
    python manage.py seed_koup --password s3cr3t

The default login is admin / admin. That is a LOCAL DEVELOPMENT convenience and
the command refuses to create it when DEBUG is off, so it can never walk into
production by accident. Pass --password (or set KOUP_ADMIN_PASSWORD) for
anything that is not your own laptop.

Why a Store and not just `createsuperuser`: this backend is multi-tenant and
every API request derives its store from the *user row*, never from client
input. A superuser with no store can open the Django admin but the Next.js
admin shows an empty shop — so the login has to belong to a tenant.

Bean prices live in Product.attributes["beans"] rather than a new column: the
loyalty currency is a customer-facing view of the same product, and a JSON key
costs no migration while the menu is still moving.
"""
import os
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.store.models import Category, Product, Store

SLUG = "koup"
NAME = "كوب"

# The glyph each category wears on its POS filter circle. Keys, not classes —
# the frontend owns the icon set and falls back to a cup for anything unknown.
CATEGORY_ICONS = {
    "قهوة": "coffee",
    "سموذي وعصائر": "cup-soda",
    "بروتين": "dumbbell",
    "فطور": "croissant",
    "حلويات": "cake",
}

#  name,                       category,          ₪,   🫘,  tags
MENU = [
    ("آيس لاتيه كراميل",        "قهوة",            18,  60, ["cold"]),
    ("آيس لاتيه بندق",          "قهوة",            18,  60, ["cold"]),
    ("سبانش لاتيه",             "قهوة",            20,  66, []),
    ("كراميل بدون سكر",         "قهوة",            19,  64, ["new", "sugar_free"]),
    ("مشروب الجوافة",           "سموذي وعصائر",     22,  74, ["cold"]),
    ("سموذي بيري",              "سموذي وعصائر",     24,  80, ["cold"]),
    ("بروتين شيك شوكولاتة",     "بروتين",           26,  86, ["new"]),
    ("بروتين شيك بيري",         "بروتين",           26,  86, []),
    ("فرنش توست بالقرفة",       "فطور",             28,  92, []),
    ("بوكس كوب",                "فطور",             75, 250, ["new", "share"]),
    ("تشيز كيك",                "حلويات",           22,  74, []),
    ("كوكيز كوب",               "حلويات",           12,  40, []),
]


class Command(BaseCommand):
    help = "Seed the كوب store, a dev admin login, and the menu."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Delete كوب's products and categories first.")
        parser.add_argument("--username", default="admin")
        parser.add_argument("--password", default=None,
                            help="Defaults to KOUP_ADMIN_PASSWORD, else 'admin' (DEBUG only).")

    @transaction.atomic
    def handle(self, *args, **opts):
        User = get_user_model()
        username = opts["username"]
        password = opts["password"] or os.getenv("KOUP_ADMIN_PASSWORD") or "admin"

        if password == "admin" and not settings.DEBUG:
            raise CommandError(
                "Refusing to create the admin/admin login with DEBUG off.\n"
                "Pass --password, or set KOUP_ADMIN_PASSWORD."
            )

        store, made = Store.objects.get_or_create(
            slug=SLUG,
            defaults={"name": NAME, "phone": "0597020201", "address": "شارع ٢٢، قلقيلية"},
        )
        self.stdout.write(("  created store " if made else "  store exists ") + f"{NAME} ({SLUG})")

        if opts["reset"]:
            Product.objects.for_pharmacy(store.pk).delete()
            Category.objects.for_pharmacy(store.pk).delete()
            self.stdout.write(self.style.WARNING("  reset: كوب catalogue wiped"))

        user, made = User.objects.get_or_create(username=username, defaults={"store": store})
        user.store = store
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        if hasattr(user, "role"):
            user.role = "owner"
        user.set_password(password)
        user.save()
        self.stdout.write(("  created login " if made else "  updated login ")
                          + f"{username} / {password}")

        product_fields = {f.name for f in Product._meta.get_fields()}
        cats, n_new, n_upd = {}, 0, 0
        for name, cat_name, ils, beans, tags in MENU:
            if cat_name not in cats:
                cat = Category.objects.get_or_create(store=store, name=cat_name)[0]
                icon = CATEGORY_ICONS.get(cat_name, "")
                if icon and cat.icon != icon:
                    cat.icon = icon
                    cat.save(update_fields=["icon"])
                cats[cat_name] = cat

            defaults = {"price": Decimal(ils), "category": cats[cat_name]}
            if "cost" in product_fields:
                defaults["cost"] = (Decimal(ils) * Decimal("0.34")).quantize(Decimal("0.01"))
            if "stock" in product_fields:
                defaults["stock"] = Decimal(999)
            if "attributes" in product_fields:
                defaults["attributes"] = {"beans": beans, "tags": tags}

            obj, made = Product.objects.get_or_create(
                store=store, name=name, defaults=defaults
            )
            if made:
                n_new += 1
            else:
                for k, v in defaults.items():
                    setattr(obj, k, v)
                obj.save()
                n_upd += 1

        self.stdout.write(f"  menu: {n_new} added, {n_upd} updated, "
                          f"{len(cats)} categories")
        self.stdout.write(self.style.SUCCESS(
            f"\n  كوب is seeded.\n"
            f"  Django admin : http://localhost:8000/admin/  ({username} / {password})\n"
            f"  Admin app    : http://localhost:3000/login   ({username} / {password})\n"
            f"  Customer app : http://localhost:3000/app\n"))

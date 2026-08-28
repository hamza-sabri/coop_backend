"""Seed the كوب tenant and its REAL menu — products with their variants.

    python manage.py seed_koup_menu
    python manage.py seed_koup_menu --reset
    python manage.py seed_koup_menu --url https://coop.clinixa.cloud/koup/menu-seed.json

Why this exists alongside `seed_koup`:

  * `seed_koup` carries the older FLAT menu, where every size and flavour was
    its own product — "شاي مثلج ليمون", "شاي مثلج خوخ" and so on as separate
    rows. That is wrong: they are one menu line with variants.
  * `seed_koup` also creates a staff login. Creating a store should not require
    minting an account, so this command deliberately creates NO users. Run
    `seed_koup --password …` yourself if you want the admin login too.

The menu itself lives in the FRONTEND repo at public/koup/menu-seed.json —
one file, transcribed from the shop's printed menu — so it is fetched over
HTTP from the deployed site rather than duplicated here and left to rot.
Pass --file to read it from disk instead.
"""
import json
import urllib.request
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.store.models import Category, Product, ProductVariant, Store

SLUG = "koup"
NAME = "كوب"
DEFAULT_URL = "https://coop.clinixa.cloud/koup/menu-seed.json"


class Command(BaseCommand):
    help = "Seed the كوب store and the real menu (products + variants). Creates no users."

    def add_arguments(self, parser):
        parser.add_argument("--store", default=SLUG)
        parser.add_argument("--url", default=DEFAULT_URL)
        parser.add_argument("--file", default=None,
                            help="Read menu-seed.json from disk instead of --url.")
        parser.add_argument("--reset", action="store_true",
                            help="Delete this store's products and categories first.")

    def _load(self, opts):
        if opts["file"]:
            with open(opts["file"], encoding="utf-8") as fh:
                return json.load(fh)
        try:
            with urllib.request.urlopen(opts["url"], timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:                                  # noqa: BLE001
            raise CommandError(f"could not fetch {opts['url']}: {exc}") from exc

    @transaction.atomic
    def handle(self, *args, **opts):
        data = self._load(opts)
        products = data.get("products") or []
        if not products:
            raise CommandError("menu-seed.json has no products")

        store, made = Store.objects.get_or_create(
            slug=opts["store"],
            defaults={"name": NAME, "phone": "0597020201",
                      "address": "شارع ٢٢، قلقيلية"},
        )
        self.stdout.write(("  created store " if made else "  store exists ")
                          + f"{store.name} ({store.slug})")

        if opts["reset"]:
            ProductVariant.unguarded.filter(product__store=store).delete()
            Product.unguarded.filter(store=store).delete()
            Category.unguarded.filter(store=store).delete()
            self.stdout.write(self.style.WARNING("  reset: catalogue wiped"))

        cats, n_prod, n_var = {}, 0, 0
        for row in products:
            cat_name = (row.get("category") or "").strip()
            cat = None
            if cat_name:
                if cat_name not in cats:
                    cats[cat_name] = Category.unguarded.get_or_create(
                        store=store, name=cat_name)[0]
                cat = cats[cat_name]

            product, _ = Product.unguarded.get_or_create(
                store=store, name=row["name"],
                defaults={"price": Decimal(str(row.get("price", 0)))},
            )
            product.price = Decimal(str(row.get("price", 0)))
            product.category = cat
            # Relative paths like /koup/menu/x.webp are served by the frontend.
            # The model field is a URLField, but .save() does not run validators
            # — full_clean() would reject these, which is why nothing calls it.
            product.image = row.get("image", "") or ""
            product.save()
            n_prod += 1

            for v in row.get("variants") or []:
                variant, _ = ProductVariant.unguarded.get_or_create(
                    product=product, label=v["label"],
                    defaults={"price": Decimal(str(v.get("price", 0)))},
                )
                variant.price = Decimal(str(v.get("price", 0)))
                variant.save()
                n_var += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n  {n_prod} products, {n_var} variants, {len(cats)} categories.\n"
            f"  Check: /api/v1/public/menu/?store={store.slug}\n"))

"""Remove everything `seed_showcase` created — and only that.

    python manage.py purge_showcase              # show what would go, touch nothing
    python manage.py purge_showcase --yes        # delete it
    python manage.py purge_showcase --store coop --yes

Works from the DemoMark registry, never from guesses ("customers named like a
demo", "sales before a date"): a real sale rung during the demo period, a real
customer, a cost the owner typed — none of them are in the registry, so none
of them can be touched. Drink costs the seed filled in are put back to what
they were (zero). Points bands the seed created are kept: they are settings.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.store import models

#: Delete order: things that point at others go first.
ORDER = [
    ("store.SaleReturn", "المرتجعات"),
    ("store.Sale", "الفواتير"),
    ("store.Customer", "الزبائن"),
    ("store.RecipeLine", "سطور الوصفات"),
    ("store.InventoryItem", "أصناف المخزون (وحركاتها)"),
    ("store.Supplier", "الموردون"),
    ("store.Expense", "المصاريف"),
    ("store.RecurringExpense", "المصاريف الشهرية الثابتة"),
    ("store.Shift", "الورديات"),
    ("accounts.User", "الموظفون التجريبيون"),
]
RESTORES = [
    ("cost:store.Product", models.Product, "تكلفة الأصناف"),
    ("cost:store.ProductVariant", models.ProductVariant, "تكلفة الأحجام"),
]


def _model(label):
    if label == "accounts.User":
        return get_user_model()
    return getattr(models, label.split(".", 1)[1])


class Command(BaseCommand):
    help = "Delete showcase data created by seed_showcase (dry run unless --yes)."

    def add_arguments(self, parser):
        parser.add_argument("--store", default=None, help="store slug (default: this deployment's shop)")
        parser.add_argument("--yes", action="store_true", help="actually delete")

    def handle(self, *args, **o):
        from apps.store.management.commands._store import resolve_store

        store = resolve_store(o["store"])
        marks = models.DemoMark.objects.for_pharmacy(store)
        by_model = defaultdict(list)
        restore = defaultdict(list)
        files = []
        for m in marks.values("model", "object_pk", "restore"):
            if m["model"] == "file:storage":
                key = (m["restore"] or {}).get("key")
                if key:
                    files.append(key)
            elif m["model"].startswith("cost:"):
                restore[m["model"]].append((m["object_pk"], m["restore"] or {}))
            else:
                by_model[m["model"]].append(m["object_pk"])

        if not by_model and not restore:
            self.stdout.write("no showcase data in this shop.")
            return

        for label, name in ORDER:
            self.stdout.write(f"  {name}: {len(by_model.get(label, []))}")
        for key, _, name in RESTORES:
            self.stdout.write(f"  {name} تعود لقيمتها السابقة: {len(restore.get(key, []))}")
        self.stdout.write(f"  الصور: {len(files)}")
        if not o["yes"]:
            self.stdout.write(self.style.WARNING("dry run — add --yes to delete"))
            return

        with transaction.atomic():
            for label, name in ORDER:
                ids = by_model.get(label, [])
                if not ids:
                    continue
                model = _model(label)
                if label == "store.InventoryItem":
                    # A recipe line the owner added on a showcase ingredient
                    # would block the delete (PROTECT); the ingredient is
                    # going, so its lines go with it — said out loud.
                    extra = models.RecipeLine.unguarded.filter(store_id=store.pk, item_id__in=ids)
                    if extra.exists():
                        self.stdout.write(f"  removing {extra.count()} recipe lines that use showcase ingredients")
                        extra.delete()
                qs = model._base_manager.filter(pk__in=ids)
                if label == "accounts.User":
                    qs = qs.filter(store=store)
                elif hasattr(model, "store_id"):
                    qs = qs.filter(store_id=store.pk)
                n, _ = qs.delete()
                self.stdout.write(f"  deleted {name}: {n}")
            for key, model, name in RESTORES:
                for pk, prev in restore.get(key, []):
                    filt = {"pk": pk}
                    if model is models.Product:
                        filt["store_id"] = store.pk
                    else:
                        filt["product__store_id"] = store.pk
                    model._base_manager.filter(**filt).update(cost=Decimal(prev.get("cost") or "0"))
            marks.delete()

        # Pictures last, once the rows that pointed at them are gone. A file
        # that is already missing is not an error.
        from django.core.files.storage import default_storage

        gone = 0
        for key in files:
            try:
                default_storage.delete(key)
                gone += 1
            except Exception:
                pass
        if files:
            self.stdout.write(f"  deleted pictures: {gone}")

        from apps.store.views import (
            invalidate_customers_quick_cache, invalidate_reports_cache,
            invalidate_sales_stats_cache,
        )
        invalidate_reports_cache(store.pk)
        invalidate_sales_stats_cache(store.pk)
        invalidate_customers_quick_cache(store.pk)
        self.stdout.write(self.style.SUCCESS("showcase data removed."))

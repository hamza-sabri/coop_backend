"""Open the shop for real: everything from testing goes, the setup stays.

    python manage.py go_live coop              # dry run — counts only
    python manage.py go_live coop --yes        # do it

Goes:
  sales, returns, invoice history, app orders, customers, points, debts,
  stock movements, expenses (one-off AND monthly), notifications, logs,
  the demo employees and demo shifts the showcase made, their photos,
  the open POS carts, and the drink costs the showcase invented (put back
  to what they were before it — usually empty, so the owner types the real
  ones; pass --keep-demo-costs to leave them).

Stays:
  the menu (drinks, sizes, pictures, categories), the POS quick cards,
  the inventory items (stock set to 0, expiry dates cleared), recipes,
  points tiers, expense categories, shifts the shop made itself, the
  owner's and real employees' accounts, the store's settings.

One transaction for the database. Photo files are deleted after it commits.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.store import models
from apps.store import reset as reset_service


class Command(BaseCommand):
    help = "Wipe all test data and keep the menu, inventory items and setup."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Store slug, e.g. coop")
        parser.add_argument("--yes", action="store_true", help="Actually delete (otherwise a dry run).")
        parser.add_argument("--keep-demo-costs", action="store_true",
                            help="Leave the drink costs the showcase filled in.")
        parser.add_argument("--firebase", action="store_true",
                            help="Also delete the customers' Firebase accounts.")

    def handle(self, *args, **o):
        try:
            store = models.Store.objects.get(slug=o["slug"])
        except models.Store.DoesNotExist as exc:
            raise CommandError(f"No store with slug {o['slug']!r}") from exc

        User = get_user_model()
        marks = list(models.DemoMark.unguarded.filter(store_id=store.pk).values("model", "object_pk", "restore"))
        demo_users = [m["object_pk"] for m in marks if m["model"] == "accounts.User"]
        demo_shifts = [m["object_pk"] for m in marks if m["model"] == "store.Shift"]
        files = [(m["restore"] or {}).get("key") for m in marks if m["model"] == "file:storage"]
        files = [f for f in files if f]
        costs = [m for m in marks if m["model"] in ("cost:store.Product", "cost:store.ProductVariant")]

        users_q = User.objects.filter(pk__in=demo_users, store=store, is_superuser=False)
        shifts_q = models.Shift.unguarded.filter(store_id=store.pk, pk__in=demo_shifts)
        recurring_q = models.RecurringExpense.unguarded.filter(store_id=store.pk)
        carts_q = models.PosCartState.objects.filter(user__store=store)
        items_q = models.InventoryItem.unguarded.filter(store_id=store.pk)
        uids = reset_service.firebase_uids(store)

        self.stdout.write(self.style.WARNING(f"\n{store.name} ({store.slug}) — will delete:"))
        for label, n in reset_service.preview(store):
            self.stdout.write(f"  {label}: {n}")
        self.stdout.write(f"  المصاريف الشهرية الثابتة: {recurring_q.count()}")
        self.stdout.write(f"  الموظفون التجريبيون: {users_q.count()}  ({', '.join(users_q.values_list('username', flat=True))})")
        self.stdout.write(f"  الورديات التجريبية: {shifts_q.count()}")
        self.stdout.write(f"  سلال نقاط البيع المفتوحة: {carts_q.count()}")
        self.stdout.write(f"  صور تجريبية: {len(files)}")
        if o["keep_demo_costs"]:
            self.stdout.write("  تكاليف المشروبات التجريبية: تبقى (--keep-demo-costs)")
        else:
            self.stdout.write(f"  تكاليف المشروبات التجريبية تعود كما كانت: {len(costs)}")
        self.stdout.write(f"  أصناف المخزون تبقى ويُصفَّر رصيدها: {items_q.count()}")
        self.stdout.write(f"  حسابات فايربيس: {len(uids) if o['firebase'] else 'لا (أضف --firebase)'}")
        self.stdout.write("\nيبقى: المنيو والصور والتصنيفات، أصناف المخزون والوصفات، شرائح النقاط، "
                          "تصنيفات المصاريف، حساب المالك والموظفين الحقيقيين، إعدادات المتجر.")

        if not o["yes"]:
            self.stdout.write(self.style.SUCCESS("\nDry run — nothing was deleted. Add --yes to do it."))
            return

        with transaction.atomic():
            if not o["keep_demo_costs"]:
                for m in costs:
                    model = models.Product if m["model"] == "cost:store.Product" else models.ProductVariant
                    filt = {"pk": m["object_pk"]}
                    filt["store_id" if model is models.Product else "product__store_id"] = store.pk
                    model._base_manager.filter(**filt).update(cost=Decimal((m["restore"] or {}).get("cost") or "0"))
            done = reset_service.wipe_database(store)  # also deletes the DemoMark rows
            n_rec, _ = recurring_q.delete()
            n_carts, _ = carts_q.delete()
            # Shifts and staff after the sales that pointed at them are gone.
            n_shifts, _ = shifts_q.delete()
            n_users, _ = users_q.delete()
            items_q.update(stock=0, expiry_date=None)

        from django.core.files.storage import default_storage

        gone = 0
        for key in files:
            try:
                default_storage.delete(key)
                gone += 1
            except Exception:  # noqa: BLE001 — a missing file is not an error
                pass

        from apps.store import views

        for fn in ("invalidate_dashboard_cache", "invalidate_med_stats_cache", "invalidate_sales_stats_cache",
                   "invalidate_pos_catalog_cache", "invalidate_customers_quick_cache", "invalidate_reports_cache"):
            try:
                getattr(views, fn)(store.pk)
            except Exception:  # noqa: BLE001
                pass

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Deleted {sum(n for _, n in done)} history rows, {n_rec} monthly expenses, "
            f"{n_users} demo employees, {n_shifts} demo shifts, {n_carts} open carts, {gone} photos. "
            f"Inventory zeroed."
        ))
        if o["firebase"]:
            n, note = reset_service.delete_firebase_users(uids)
            self.stdout.write((self.style.SUCCESS if n else self.style.WARNING)(note))

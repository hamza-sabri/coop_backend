"""seed_showcase / purge_showcase: the purge removes exactly what the seed
made. A real sale, a real customer and a cost the owner typed survive it."""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.accounts.models import User
from apps.store import models


class ShowcaseTests(TestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="كوب", slug="koup")
        self.owner = User.objects.create_user(username="o", password="x", store=self.store)
        cat = models.Category.objects.create(store=self.store, name="قهوة")
        self.typed = models.Product.objects.create(
            store=self.store, name="لاتيه", price=Decimal("15"), cost=Decimal("5"), category=cat
        )
        self.blank = models.Product.objects.create(
            store=self.store, name="موكا", price=Decimal("18"), cost=Decimal("0"), category=cat
        )
        self.real_customer = models.Customer.objects.create(store=self.store, name="حقيقي", phone="0590000000")
        self.real_sale = models.Sale.objects.create(store=self.store, total=Decimal("15"), discounted_total=Decimal("15"))

    def seed(self):
        call_command("seed_showcase", "--days", "8", "--customers", "6", "--sales", "300", "--no-pictures", stdout=StringIO())

    def test_seed_then_purge_leaves_only_what_was_real(self):
        self.seed()
        S = models.Sale.objects.for_pharmacy(self.store)
        self.assertGreater(S.count(), 50)
        self.blank.refresh_from_db()
        self.assertGreater(self.blank.cost, 0, "a drink without a cost gets one for the show")
        self.assertTrue(models.InventoryItem.objects.for_pharmacy(self.store).exists())
        self.assertTrue(models.RecurringExpense.objects.for_pharmacy(self.store).exists())

        call_command("purge_showcase", "--yes", stdout=StringIO())
        self.assertEqual(list(S.values_list("pk", flat=True)), [self.real_sale.pk])
        self.assertEqual(
            list(models.Customer.objects.for_pharmacy(self.store).values_list("pk", flat=True)),
            [self.real_customer.pk],
        )
        self.blank.refresh_from_db()
        self.typed.refresh_from_db()
        self.assertEqual(self.blank.cost, Decimal("0"), "the seeded cost is put back")
        self.assertEqual(self.typed.cost, Decimal("5"), "a cost the owner typed is never touched")
        self.assertFalse(models.InventoryItem.objects.for_pharmacy(self.store).exists())
        self.assertFalse(models.Expense.objects.for_pharmacy(self.store).exists())
        self.assertFalse(models.DemoMark.objects.for_pharmacy(self.store).exists())
        self.assertFalse(User.objects.filter(username__startswith="demo-").exists())
        # Bands are settings: kept.
        self.assertTrue(models.EarnRule.objects.for_pharmacy(self.store).exists())

    def test_seeding_twice_is_refused(self):
        self.seed()
        with self.assertRaises(CommandError):
            self.seed()

    def test_purge_without_yes_touches_nothing(self):
        self.seed()
        n = models.Sale.objects.for_pharmacy(self.store).count()
        call_command("purge_showcase", stdout=StringIO())
        self.assertEqual(models.Sale.objects.for_pharmacy(self.store).count(), n)


class StoreResolutionTests(TestCase):
    def test_uses_the_deployments_shop_then_refuses_to_guess(self):
        from django.test import override_settings

        from apps.store.management.commands._store import resolve_store

        coop = models.Store.objects.create(name="كوب", slug="coop")
        with override_settings(CLERK_STORE_SLUG="coop"):
            self.assertEqual(resolve_store(None), coop)
        models.Store.objects.create(name="آخر", slug="other")
        with override_settings(CLERK_STORE_SLUG="missing"):
            with self.assertRaises(CommandError):
                resolve_store(None)
        self.assertEqual(resolve_store("other").slug, "other")
        with self.assertRaises(CommandError):
            resolve_store("koup")


class GoLiveTests(TestCase):
    """go_live: every test row goes; the menu, the inventory list (zeroed) and
    the setup stay; the drink costs the showcase invented are put back."""

    def setUp(self):
        self.store = models.Store.objects.create(name="كوب", slug="koup")
        self.owner = User.objects.create_user(username="o", password="x", store=self.store)
        cat = models.Category.objects.create(store=self.store, name="قهوة")
        self.typed = models.Product.objects.create(
            store=self.store, name="لاتيه", price=Decimal("15"), cost=Decimal("5"), category=cat, image="x.jpg"
        )
        self.blank = models.Product.objects.create(
            store=self.store, name="موكا", price=Decimal("18"), cost=Decimal("0"), category=cat
        )
        self.own_shift = None
        self.real_emp = User.objects.create_user(username="real", password="x", store=self.store, role="employee")
        call_command("seed_showcase", "--days", "8", "--customers", "6", "--sales", "200", "--no-pictures", stdout=StringIO())
        self.other = models.Store.objects.create(name="آخر", slug="other")
        self.other_sale = models.Sale.objects.create(store=self.other, total=Decimal("9"), discounted_total=Decimal("9"))

    def test_dry_run_touches_nothing(self):
        n = models.Sale.objects.for_pharmacy(self.store).count()
        call_command("go_live", "koup", stdout=StringIO())
        self.assertEqual(models.Sale.objects.for_pharmacy(self.store).count(), n)

    def test_go_live_keeps_the_setup_and_drops_the_rest(self):
        call_command("go_live", "koup", "--yes", stdout=StringIO())
        st = self.store
        for m in (models.Sale, models.SaleItem, models.SaleReturn, models.Customer, models.LoyaltyProfile,
                  models.BeanLedger, models.StockMove, models.Expense, models.RecurringExpense, models.DemoMark,
                  models.Order, models.Shift):
            field = "sale__store_id" if m is models.SaleItem else "store_id"
            self.assertFalse(m.unguarded.filter(**{field: st.pk}).exists(), m.__name__)
        # Setup stays.
        self.assertEqual(models.Product.objects.for_pharmacy(st).count(), 2)
        self.typed.refresh_from_db()
        self.blank.refresh_from_db()
        self.assertEqual(self.typed.cost, Decimal("5"))
        self.assertEqual(self.typed.image, "x.jpg")
        self.assertEqual(self.blank.cost, Decimal("0"), "an invented cost is put back")
        self.assertTrue(models.Category.objects.for_pharmacy(st).exists())
        self.assertTrue(models.EarnRule.objects.for_pharmacy(st).exists())
        self.assertTrue(models.ExpenseCategory.objects.for_pharmacy(st).exists())
        items = models.InventoryItem.objects.for_pharmacy(st)
        self.assertTrue(items.exists())
        self.assertFalse(items.exclude(stock=0).exists())
        self.assertFalse(items.filter(expiry_date__isnull=False).exists())
        # People: demo staff gone, the owner and a real employee stay.
        self.assertFalse(User.objects.filter(username__startswith="demo-").exists())
        self.assertTrue(User.objects.filter(pk__in=[self.owner.pk, self.real_emp.pk]).count() == 2)
        # Another shop is never touched.
        self.assertTrue(models.Sale.unguarded.filter(pk=self.other_sale.pk).exists())

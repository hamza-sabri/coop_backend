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
        call_command("seed_showcase", "--days", "8", "--customers", "6", stdout=StringIO())

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

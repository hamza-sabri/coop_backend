"""/shop/me/ → stats: the customer's own dashboard numbers."""
from decimal import Decimal

from apps.store import models
from apps.store.tests.test_app_auth import Base


class MeStats(Base):
    def test_numbers_over_all_visits(self):
        self.otp()
        api = self.as_customer(self.verify().json()["token"])
        c = models.Customer.objects.for_pharmacy(self.a).get()
        for total, name, q in ((Decimal("24"), "فرابتشينو", 2), (Decimal("13"), "جزر", 1), (Decimal("12"), "فرابتشينو", 1)):
            s = models.Sale.objects.create(store=self.a, customer=c, total=total, discounted_total=total)
            models.SaleItem.objects.create(sale=s, medication_name=name, category="باردة",
                                           unit_price=total / q, quantity=q, line_total=total)
        st = api.get("/api/v1/shop/me/").json()["stats"]
        self.assertEqual(st["visits"], 3)
        self.assertEqual(Decimal(st["spent"]), Decimal("49"))
        self.assertEqual(Decimal(st["avg_bill"]), Decimal("16.33"))
        self.assertEqual(st["top_items"][0], {"name": "فرابتشينو", "qty": 3})
        self.assertEqual(st["top_categories"][0]["name"], "باردة")
        self.assertEqual(len(st["months"]), 6)
        self.assertEqual(st["months"][-1]["visits"], 3)
        self.assertIsNotNone(st["usual_hour"])
        self.assertIsNotNone(st["last_visit"])

    def test_new_customer_has_empty_stats(self):
        self.otp()
        api = self.as_customer(self.verify().json()["token"])
        st = api.get("/api/v1/shop/me/").json()["stats"]
        self.assertEqual((st["visits"], st["spent"], st["top_items"], st["usual_hour"]), (0, "0", [], None))

"""Reports over an exact time range, and narrowed to one way of paying.

The owner asked for both on 24 Oct: "pick the date and time for the start and
end" and "all / cash / card" above every report.
"""
from datetime import date, timedelta
from unittest import mock

from apps.store import finance
from apps.store.tests.test_cafe_finance import Base, at

D = date(2026, 3, 10)


class TimedRangeTests(Base):
    def setUp(self):
        super().setUp()
        # 09:00, 14:00, 20:00 on the 10th and 01:00 the next night (still the 10th's business day)
        for hh, day in ((9, D), (14, D), (20, D), (1, D + timedelta(days=1))):
            with mock.patch("django.utils.timezone.now", return_value=at(day, hh)):
                self.sell([(self.latte, 1)])

    def pnl(self, qs):
        with mock.patch("apps.store.finance.today", return_value=D + timedelta(days=2)):
            r = self.api.get(f"/api/v1/reports/pnl/?period=custom&{qs}")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_a_bare_date_range_is_whole_business_days_as_before(self):
        p = self.pnl(f"start={D}&end={D}")
        self.assertEqual(p["lines"]["net_revenue"], "48.00")

    def test_start_and_end_times_cut_the_range_exactly(self):
        p = self.pnl(f"start={D}T13:00&end={D}T21:00")
        self.assertEqual(p["lines"]["net_revenue"], "24.00")  # 14:00 and 20:00
        self.assertEqual(p["kpis"]["tickets"], 2)

    def test_the_end_moment_is_not_included(self):
        p = self.pnl(f"start={D}T09:00&end={D}T14:00")
        self.assertEqual(p["kpis"]["tickets"], 1)

    def test_a_range_across_midnight(self):
        p = self.pnl(f"start={D}T19:00&end={D + timedelta(days=1)}T02:00")
        self.assertEqual(p["kpis"]["tickets"], 2)  # 20:00 and 01:00

    def test_reversed_times_are_swapped(self):
        p = self.pnl(f"start={D}T21:00&end={D}T13:00")
        self.assertEqual(p["kpis"]["tickets"], 2)

    def test_the_comparison_is_the_same_length_just_before(self):
        r = finance.resolve_range({"period": "custom", "start": f"{D}T13:00", "end": f"{D}T21:00"})
        ps, pe = finance.previous_range(r["start"], r["end"], "custom")
        lo, hi = finance.bounds(ps, pe)
        self.assertEqual((lo, hi), (at(D, 5), at(D, 13)))
        # 09:00 falls in 05:00–13:00
        self.assertEqual(self.pnl(f"start={D}T13:00&end={D}T21:00")["previous"]["tickets"], 1)

    def test_every_tab_takes_the_times(self):
        q = f"?period=custom&start={D}T13:00&end={D}T21:00"
        with mock.patch("apps.store.finance.today", return_value=D + timedelta(days=2)):
            items = self.api.get("/api/v1/reports/items/" + q).json()
            hours = self.api.get("/api/v1/reports/hours/" + q).json()
            for u in ("times", "shifts", "customers", "returns"):
                self.assertEqual(self.api.get(f"/api/v1/reports/{u}/" + q).status_code, 200, u)
            self.assertEqual(self.api.get(f"/api/v1/reports/items/{self.latte.pk}/" + q).status_code, 200)
        latte = next(i for i in items["items"] if i["product_id"] == self.latte.pk)
        self.assertEqual(latte["qty"], "2.00")
        self.assertEqual(sorted(c["hour"] for c in hours["cells"]), [14, 20])


class PaymentFilterTests(Base):
    def setUp(self):
        super().setUp()
        with mock.patch("django.utils.timezone.now", return_value=at(D, 10)):
            self.sell([(self.latte, 2)])                              # cash 24
            self.sell([(self.latte, 1)], payment_method="card")       # card 12
            self.sell([(self.water, 1)], payment_method="card")       # card 3

    def get(self, path, pay=""):
        with mock.patch("apps.store.finance.today", return_value=D):
            r = self.api.get(f"/api/v1/reports/{path}/?period=day&date={D}" + (f"&pay={pay}" if pay else ""))
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_all_cash_and_card(self):
        self.assertEqual(self.get("pnl")["lines"]["net_revenue"], "39.00")
        cash = self.get("pnl", "cash")
        card = self.get("pnl", "card")
        self.assertEqual(cash["lines"]["net_revenue"], "24.00")
        self.assertEqual(card["lines"]["net_revenue"], "15.00")
        self.assertEqual(cash["kpis"]["tickets"], 1)
        self.assertEqual(card["kpis"]["tickets"], 2)
        self.assertEqual(cash["pay"], "cash")
        self.assertIsNone(self.get("pnl")["pay"])

    def test_an_unknown_payment_means_all(self):
        self.assertEqual(self.get("pnl", "haat")["lines"]["net_revenue"], "39.00")

    def test_tabs_follow_the_filter(self):
        items = self.get("items", "card")["items"]
        self.assertEqual({i["product_id"]: i["qty"] for i in items if i["qty"] != "0.00"}, {self.latte.pk: "1.00", self.water.pk: "1.00"})
        cash_items = self.get("items", "cash")["items"]
        self.assertEqual({i["product_id"]: i["qty"] for i in cash_items if i["qty"] != "0.00"}, {self.latte.pk: "2.00"})
        cells = self.get("hours", "cash")["cells"]
        self.assertEqual(sum(float(c["qty"]) for c in cells), 2.0)

    def test_returns_follow_the_sale_they_came_from(self):
        from apps.store import models
        sale = models.Sale.objects.for_pharmacy(self.store).filter(payment_method="card", items__product=self.latte).first()
        with mock.patch("django.utils.timezone.now", return_value=at(D, 11)):
            r = self.api.post(f"/api/v1/sales/{sale.pk}/returns/",
                              {"sale_item": sale.items.get().pk, "quantity": "1", "reason": "late", "refund": "full"},
                              format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.get("pnl", "card")["lines"]["net_revenue"], "3.00")
        self.assertEqual(self.get("pnl", "cash")["lines"]["net_revenue"], "24.00")
        self.assertEqual(self.get("returns", "cash")["count"], 0)
        self.assertEqual(self.get("returns", "card")["count"], 1)

    def test_shop_wide_costs_stay_out_of_a_narrowed_report(self):
        from apps.store import models
        cat = models.ExpenseCategory.objects.for_pharmacy(self.store).first()
        if cat is None:
            finance.ensure_default_categories(self.store.pk)
            cat = models.ExpenseCategory.objects.for_pharmacy(self.store).first()
        models.Expense.objects.create(store=self.store, category=cat, amount="310", period=D.replace(day=1))
        everything = self.get("pnl")
        cash = self.get("pnl", "cash")
        self.assertNotEqual(everything["lines"]["opex"], "0.00")
        self.assertEqual(cash["lines"]["opex"], "0.00")
        self.assertEqual(cash["lines"]["net_profit"], cash["lines"]["gross_profit"])

    def test_the_filter_never_leaks_into_the_next_request(self):
        self.get("pnl", "card")
        self.assertIsNone(finance.PAY.get())
        self.assertEqual(self.get("pnl")["lines"]["net_revenue"], "39.00")

"""Every number the owner reads, checked by hand against one small day.

The day (all at fixed hours on one business date):

  10:00  sale 1   2 × A (10) + 1 × B (20)          = 40, cash
  11:00  sale 2   1 × B, cashier knocks it to 18    gross 20, paid 18
  15:00  sale 3   1 × A to a customer who pays 30 points (= 3 ₪)
                                                    gross 10, paid 7
  16:00  B from sale 1 comes back, money refunded   −20
  16:30  one A from sale 1 is remade, no refund     costs another 3
  17:00  waste                                      5 ₪ of stock

  A costs 3, B costs 8 (typed by the owner).
  One expense of 310 ₪ for the month (October has 31 days → 10 ₪ a day).

By hand:
  sold at menu price      70     (40 + 20 + 10)
  discount                 2     (20 → 18)
  points                   3
  refunds                 20
  cash in the till        45     (70 − 2 − 3 − 20)
  ingredients             25     (A: 3 × 3 = 9, B: 2 × 8 = 16)
  waste + remake           8     (5 + 3)
  expenses (1 day)        10
  profit                   2     (45 − 25 − 8 − 10)

Every report that shows one of these numbers must show THIS number.
"""
from datetime import date, datetime, time
from decimal import Decimal as D

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.store import finance, models

DAY = date(2026, 10, 7)  # a Wednesday, in the past relative to the suite's clock


def at(h, m=0):
    return timezone.make_aware(datetime.combine(DAY, time(h, m)))


class DayOfNumbersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.store = models.Store.objects.create(name="كوب", slug="koup")
        cls.owner = User.objects.create_user(username="o", password="x", store=cls.store)
        cat = models.Category.objects.create(store=cls.store, name="قهوة")
        cls.A = models.Product.objects.create(store=cls.store, name="A", price=D("10"), cost=D("3"), category=cat)
        cls.B = models.Product.objects.create(store=cls.store, name="B", price=D("20"), cost=D("8"), category=cat)
        cls.cust = models.Customer.objects.create(store=cls.store, name="زبون", phone="0590000001")
        finance.ensure_default_categories(cls.store.pk)
        # Two shifts that cover the whole business day between them.
        models.Shift.objects.create(store=cls.store, name="صباحي", start=time(4), end=time(14), wage_per_day=D("0"))
        models.Shift.objects.create(store=cls.store, name="مسائي", start=time(14), end=time(4), wage_per_day=D("0"))

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        from apps.store import points as points_service

        points_service.adjust(self.store, self.cust, 100, "رصيد اختبار", key="t-seed")

        def sell(items, when, **extra):
            r = self.api.post("/api/v1/sales/", {"items": items, **extra}, format="json")
            self.assertEqual(r.status_code, 201, r.content)
            body = r.json().get("data", r.json())
            models.Sale.objects.for_pharmacy(self.store).filter(pk=body["id"]).update(created_at=when)
            return body

        self.s1 = sell([{"product": self.A.pk, "quantity": 2}, {"product": self.B.pk, "quantity": 1}], at(10))
        self.s2 = sell([{"product": self.B.pk, "quantity": 1}], at(11), discounted_total="18.00")
        self.s3 = sell([{"product": self.A.pk, "quantity": 1}], at(15), customer=self.cust.pk, beans_spent=30)

        def give_back(sale, product, refund, when):
            line = next(i for i in sale["items"] if i["product"] == product.pk)
            r = self.api.post(f"/api/v1/sales/{sale['id']}/returns/",
                              {"sale_item": line["id"], "quantity": 1, "refund": refund, "reason": "other"},
                              format="json")
            self.assertEqual(r.status_code, 201, r.content)
            models.SaleReturn.objects.for_pharmacy(self.store).filter(sale_id=sale["id"]).order_by("-pk")[:1]
            last = models.SaleReturn.objects.for_pharmacy(self.store).order_by("-pk").first()
            models.SaleReturn.objects.for_pharmacy(self.store).filter(pk=last.pk).update(created_at=when)

        give_back(self.s1, self.B, "full", at(16))
        give_back(self.s1, self.A, "none", at(16, 30))

        item = models.InventoryItem.objects.create(store=self.store, name="حليب", purchase_unit="piece",
                                                   purchase_qty=D("1"), purchase_cost=D("5"))
        models.InventoryItem.objects.for_pharmacy(self.store).filter(pk=item.pk).update(stock=D("10"))
        mv = models.StockMove.objects.create(store=self.store, item=item, kind="waste", quantity=D("-1"),
                                             unit_cost=D("5"), total_cost=D("5"), stock_after=D("9"))
        models.StockMove.objects.for_pharmacy(self.store).filter(pk=mv.pk).update(created_at=at(17))

        rent = models.ExpenseCategory.objects.for_pharmacy(self.store).get(key="rent")
        models.Expense.objects.create(store=self.store, category=rent, amount=D("310"), period=date(2026, 10, 1))

    def get(self, url):
        r = self.api.get(url)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        return body.get("data", body) if isinstance(body, dict) else body

    q = f"?period=custom&start={DAY}&end={DAY}"

    # ── the statement ──────────────────────────────────────────────────
    def test_profit_statement(self):
        p = self.get("/api/v1/reports/pnl/" + self.q)
        L = p["lines"]
        self.assertEqual(L["gross_sales"], "70.00")
        self.assertEqual(L["discounts"], "2.00")
        self.assertEqual(L["points_redeemed"], "3.00")
        self.assertEqual(L["returns"], "20.00")
        self.assertEqual(L["net_revenue"], "45.00")
        self.assertEqual(L["cogs"], "25.00")
        self.assertEqual(D(L["waste"]) + D(L["remakes"]), D("8"))
        self.assertEqual(L["opex"], "10.00")
        self.assertEqual(L["net_profit"], "2.00")
        self.assertEqual(p["kpis"]["tickets"], 3)

    def test_the_day_row_matches_the_statement(self):
        p = self.get("/api/v1/reports/pnl/" + self.q)
        row = next(r for r in p["series"] if r["date"] == DAY.isoformat())
        self.assertEqual(row["net_revenue"], p["lines"]["net_revenue"])
        self.assertEqual(row["net_profit"], p["lines"]["net_profit"])

    def test_a_month_view_adds_up_the_same(self):
        p = self.get(f"/api/v1/reports/pnl/?period=month&date={DAY}")
        self.assertEqual(p["lines"]["net_revenue"], "45.00")
        self.assertEqual(sum(D(r["net_revenue"]) for r in p["series"]), D("45"))

    # ── every other page that shows the same money ─────────────────────
    def test_invoices_page_total(self):
        s = self.get(f"/api/v1/sales/summary/?day_from={DAY}&day_to={DAY}")
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["total"], "45.00")

    def test_drinks_tab(self):
        it = self.get("/api/v1/reports/items/" + self.q)
        rows = {r["name"]: r for r in it["items"]}
        self.assertEqual(rows["A"]["qty"], "3.00")
        self.assertEqual(rows["B"]["qty"], "2.00")
        self.assertEqual(it["totals"]["revenue"], "70.00", "drinks are counted at menu price")
        # Profit per drink at menu price, before till discounts: 70 − 25.
        self.assertEqual(it["totals"]["profit"], "45.00")

    def test_shifts_add_up_to_the_day(self):
        sh = self.get("/api/v1/reports/shifts/" + self.q)["shifts"]
        self.assertEqual(sum(D(s["net_revenue"]) for s in sh), D("45"))
        self.assertEqual(sum(s["tickets"] for s in sh), 3)
        p = self.get("/api/v1/reports/pnl/" + self.q)
        self.assertEqual(sum(D(s["gross_profit"]) for s in sh), D(p["lines"]["gross_profit"]))

    def test_returns_tab(self):
        r = self.get("/api/v1/reports/returns/" + self.q)
        self.assertEqual(D(r["headline"]["refunds"] if "headline" in r else r["refunds"]), D("20"))

    def test_customers_tab(self):
        c = self.get("/api/v1/reports/customers/" + self.q)
        self.assertEqual(c["tickets"], 3)
        self.assertEqual(c["identified"], 1)
        top = c["top"][0]
        self.assertEqual(top["spend"], "7.00", "what the customer actually paid")

    def test_times_tab_best_day_is_the_cash_that_stayed(self):
        t = self.get("/api/v1/reports/times/" + self.q)
        wed = next(w for w in t["weekdays"] if w["weekday"] == "الأربعاء")
        self.assertEqual(wed["avg_revenue"], "45.00")

    def test_expenses_month(self):
        m = self.get("/api/v1/expenses/month/?month=2026-10")
        self.assertEqual(m["total"], "310.00")

    def test_salary_and_a_short_count_come_off_the_same_way(self):
        """A monthly salary (3,100 → 100 a day) and a count that finds two
        litres of milk missing (2 × 5 = 10) both reduce the day's profit."""
        sal = models.ExpenseCategory.objects.for_pharmacy(self.store).get(key="salaries")
        models.RecurringExpense.objects.create(store=self.store, category=sal, amount=D("3100"),
                                               start_month=date(2026, 10, 1), staff=self.owner)
        milk = models.InventoryItem.objects.for_pharmacy(self.store).get(name="حليب")
        models.RecipeLine.objects.create(store=self.store, product=self.A, item=milk, quantity=D("0.1"))
        mv = models.StockMove.objects.create(store=self.store, item=milk, kind="count", quantity=D("-2"),
                                             unit_cost=D("5"), total_cost=D("10"), stock_after=D("7"))
        models.StockMove.objects.for_pharmacy(self.store).filter(pk=mv.pk).update(created_at=at(18))
        p = self.get("/api/v1/reports/pnl/" + self.q)
        L = p["lines"]
        self.assertEqual(L["count_shortfall"], "10.00")
        self.assertEqual(L["opex"], "110.00")
        self.assertEqual(L["net_profit"], "-108.00")  # 45 − 25 − 8 − 10 − 110
        row = next(r for r in p["series"] if r["date"] == DAY.isoformat())
        self.assertEqual(row["net_profit"], L["net_profit"])

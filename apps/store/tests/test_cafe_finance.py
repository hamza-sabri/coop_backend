"""The café back office: costs, points bands, returns, inventory, expenses,
the P&L, and who may see any of it.

Each test pins one decision taken with the owner, so a later change that
quietly reverses it fails here with a sentence explaining what it broke.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.store import finance, models
from apps.store import points as points_service


def at(d: date, hh: int, mm: int = 0):
    return timezone.make_aware(datetime.combine(d, time(hh, mm)))


class Base(TestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="كوب", slug="koup")
        self.owner = User.objects.create_user(username="o", password="x", store=self.store)
        self.emp = User.objects.create_user(
            username="e", password="x", store=self.store, role="employee"
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.staff = APIClient()
        self.staff.force_authenticate(self.emp)
        self.cat = models.Category.objects.create(store=self.store, name="ساخن")
        self.latte = models.Product.objects.create(
            store=self.store, name="لاتيه", price=Decimal("12"), cost=Decimal("4"),
            category=self.cat, stock=Decimal("999"),
        )
        self.water = models.Product.objects.create(
            store=self.store, name="ماء", price=Decimal("3"), cost=Decimal("0"),
        )
        self.cust = models.Customer.objects.create(store=self.store, name="سامر", phone="0599")

    def sell(self, lines, client=None, **extra):
        body = {"items": [{"product": p.pk, "quantity": q} for p, q in lines], **extra}
        r = (client or self.api).post("/api/v1/sales/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return models.Sale.objects.for_pharmacy(self.store).get(pk=r.json()["id"])


# ── costs ───────────────────────────────────────────────────────────────────
class CostStampTests(Base):
    def test_cost_is_frozen_on_the_line(self):
        sale = self.sell([(self.latte, 2)])
        line = sale.items.get()
        self.assertEqual(line.unit_cost, Decimal("4"))
        self.latte.cost = Decimal("9")
        self.latte.save()
        line.refresh_from_db()
        self.assertEqual(line.unit_cost, Decimal("4"), "next month's cost must not rewrite this sale")

    def test_zero_cost_means_unknown_not_free(self):
        sale = self.sell([(self.water, 1)])
        self.assertIsNone(sale.items.get().unit_cost)

    def test_variant_cost_wins(self):
        big = models.ProductVariant.objects.create(
            product=self.latte, label="كبير", price=Decimal("15"), cost=Decimal("6")
        )
        r = self.api.post("/api/v1/sales/", {"items": [{"product": self.latte.pk, "variant": big.pk}]}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(models.SaleItem.objects.for_pharmacy(self.store).get().unit_cost, Decimal("6"))

    def test_selling_a_drink_leaves_menu_stock_alone(self):
        self.sell([(self.latte, 3)])
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.stock, Decimal("999"))


# ── points bands ────────────────────────────────────────────────────────────
class PointsBandTests(Base):
    def set_bands(self):
        r = self.api.put("/api/v1/points/rules/", {"rules": [
            {"min_total": "0", "max_total": "20", "rate_percent": "1"},
            {"min_total": "20", "max_total": "50", "rate_percent": "2"},
            {"min_total": "50", "max_total": None, "rate_percent": "5"},
        ]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_no_bands_is_the_flat_default(self):
        self.assertEqual(points_service.rate_for(self.store, Decimal("100")), Decimal("0.02"))

    def test_the_band_sets_the_rate_for_the_whole_receipt(self):
        self.set_bands()
        self.assertEqual(points_service.rate_for(self.store, Decimal("19.99")), Decimal("0.01"))
        self.assertEqual(points_service.rate_for(self.store, Decimal("20")), Decimal("0.02"))
        # ₪55 earns 5% on all of it: 2.75 ₪ → 27 points (floor of 27.5)
        self.assertEqual(points_service.points_for(Decimal("55"), store=self.store), 27)

    def test_floor_once_at_the_end(self):
        self.assertEqual(points_service.points_for(Decimal("17.50")), 3)

    def test_ledger_keeps_the_rate_and_the_sale(self):
        self.set_bands()
        sale = self.sell([(self.latte, 5)], customer=self.cust.pk)  # ₪60 → 5%
        row = models.BeanLedger.objects.for_pharmacy(self.store).get(reason="earn")
        self.assertEqual(row.delta, 30)
        self.assertEqual(row.rate_applied, Decimal("5.00"))
        self.assertEqual(row.sale_id, sale.pk)

    def test_overlapping_bands_are_refused(self):
        r = self.api.put("/api/v1/points/rules/", {"rules": [
            {"min_total": "0", "max_total": "30", "rate_percent": "1"},
            {"min_total": "20", "max_total": None, "rate_percent": "2"},
        ]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_employee_reads_but_cannot_change_bands(self):
        self.assertEqual(self.staff.get("/api/v1/points/rules/").status_code, 200)
        r = self.staff.put("/api/v1/points/rules/", {"rules": []}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_preview_matches_the_server(self):
        self.set_bands()
        r = self.staff.get("/api/v1/points/preview/?amount=60")
        self.assertEqual(r.json()["points"], 30)
        self.assertEqual(r.json()["rate_percent"], "5.00")


# ── returns ─────────────────────────────────────────────────────────────────
class ReturnTests(Base):
    def ret(self, sale, line, client=None, **kw):
        body = {"sale_item": line.pk, "reason": "taste", **kw}
        return (client or self.api).post(f"/api/v1/sales/{sale.pk}/returns/", body, format="json")

    def test_full_refund_is_the_line_share_and_points_follow(self):
        sale = self.sell([(self.latte, 2), (self.water, 2)], customer=self.cust.pk)  # 30 ₪
        earned = points_service.earned_on(sale)
        self.assertEqual(earned, 6)  # 2% of 30 → 0.6 ₪ → 6 points
        latte_line = sale.items.get(product=self.latte)
        r = self.ret(sale, latte_line, quantity="1")
        self.assertEqual(r.status_code, 201, r.content)
        rr = models.SaleReturn.objects.for_pharmacy(self.store).get()
        self.assertEqual(rr.refund_amount, Decimal("12.00"))
        self.assertEqual(rr.cost_written_off, Decimal("4.00"))
        self.assertEqual(rr.points_reversed, 2)  # floor(6 × 12/30)
        self.assertEqual(points_service.balance_of(self.cust), 4)

    def test_remake_refunds_nothing(self):
        sale = self.sell([(self.latte, 1)], customer=self.cust.pk)
        self.ret(sale, sale.items.get(), refund="none", reason="spilled")
        rr = models.SaleReturn.objects.for_pharmacy(self.store).get()
        self.assertEqual(rr.refund_amount, Decimal("0.00"))
        self.assertEqual(points_service.balance_of(self.cust), 2)

    def test_cannot_return_more_than_was_sold(self):
        sale = self.sell([(self.latte, 1)])
        line = sale.items.get()
        self.assertEqual(self.ret(sale, line).status_code, 201)
        self.assertEqual(self.ret(sale, line).status_code, 400)

    def test_retry_with_the_same_uuid_records_once(self):
        sale = self.sell([(self.latte, 2)])
        line = sale.items.get()
        self.ret(sale, line, quantity="1", client_uuid="abc", client=self.staff)
        self.ret(sale, line, quantity="1", client_uuid="abc", client=self.staff)
        self.assertEqual(models.SaleReturn.objects.for_pharmacy(self.store).count(), 1)

    def test_returned_quantity_shows_on_the_invoice(self):
        sale = self.sell([(self.latte, 2)])
        self.ret(sale, sale.items.get(), quantity="1")
        data = self.api.get(f"/api/v1/sales/{sale.pk}/").json()
        self.assertEqual(data["items"][0]["returned_quantity"], "1.000")
        self.assertEqual(data["refunded_total"], "12.00")
        self.assertEqual(data["returns"][0]["reason_label"], "الطعم")


# ── inventory ───────────────────────────────────────────────────────────────
class InventoryTests(Base):
    def make(self, **kw):
        body = {"name": "حليب", "purchase_qty": "1", "purchase_unit": "l",
                "purchase_cost": "5", "opening_stock": "3", "reorder_level": "1000", **kw}
        r = self.api.post("/api/v1/inventory-items/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return models.InventoryItem.objects.for_pharmacy(self.store).get(pk=r.json()["id"])

    def test_units_and_cost_are_derived(self):
        milk = self.make()
        self.assertEqual(milk.unit, "ml")
        self.assertEqual(milk.stock, Decimal("3000"))
        self.assertEqual(milk.unit_cost, Decimal("0.005"))

    def test_purchase_updates_cost_and_stock(self):
        milk = self.make()
        r = self.api.post(f"/api/v1/inventory-items/{milk.pk}/purchase/",
                          {"quantity": "12", "unit": "l", "cost": "48"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        milk.refresh_from_db()
        self.assertEqual(milk.stock, Decimal("15000"))
        self.assertEqual(milk.unit_cost, Decimal("0.004"))

    def test_employee_records_waste_but_never_buys_or_sees_cost(self):
        milk = self.make()
        r = self.staff.post(f"/api/v1/inventory-items/{milk.pk}/waste/",
                            {"quantity": "500", "unit": "ml", "reason": "انتهى"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertNotIn("total_cost", r.json())
        r = self.staff.post(f"/api/v1/inventory-items/{milk.pk}/purchase/",
                            {"quantity": "1", "unit": "l", "cost": "5"}, format="json")
        self.assertEqual(r.status_code, 403)
        listed = self.staff.get("/api/v1/inventory-items/").json()
        row = (listed.get("results") if isinstance(listed, dict) else listed)[0]
        self.assertNotIn("unit_cost", row)
        self.assertNotIn("purchase_cost", row)

    def test_stocktake_records_only_differences(self):
        milk = self.make()
        cups = self.make(name="أكواب", purchase_unit="piece", purchase_qty="100",
                         purchase_cost="10", opening_stock="200")
        r = self.staff.post("/api/v1/inventory-items/stocktake/", {"counts": [
            {"item": milk.pk, "counted": "2500"}, {"item": cups.pk, "counted": "200"},
        ]}, format="json")
        self.assertEqual(r.json()["moves"], 1)
        milk.refresh_from_db()
        self.assertEqual(milk.stock, Decimal("2500"))


# ── expenses and the statement ──────────────────────────────────────────────
class PnlTests(Base):
    def setUp(self):
        super().setUp()
        finance.ensure_default_categories(self.store.pk)
        self.rent = models.ExpenseCategory.objects.for_pharmacy(self.store).get(key="rent")
        self.elec = models.ExpenseCategory.objects.for_pharmacy(self.store).get(key="electricity")

    def test_fixed_costs_prorate_by_day(self):
        models.RecurringExpense.objects.create(
            store=self.store, category=self.rent, amount=Decimal("3100"), start_month=date(2026, 1, 1)
        )
        models.Expense.objects.create(
            store=self.store, category=self.elec, amount=Decimal("310"), period=date(2026, 3, 1)
        )
        day = finance.opex(self.store.pk, date(2026, 3, 10), date(2026, 3, 10))
        self.assertEqual(day["total"], Decimal("110.00"))  # 100 rent + 10 electricity
        month = finance.opex(self.store.pk, date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual(month["total"], Decimal("3410.00"))

    def test_the_statement_adds_up(self):
        d = date(2026, 3, 10)
        with mock.patch("django.utils.timezone.now", return_value=at(d, 18)):
            sale = self.sell([(self.latte, 2), (self.water, 2)], customer=self.cust.pk)  # 30
        with mock.patch("django.utils.timezone.now", return_value=at(d, 19)):
            self.api.post(f"/api/v1/sales/{sale.pk}/returns/",
                          {"sale_item": sale.items.get(product=self.latte).pk, "quantity": "1",
                           "reason": "taste"}, format="json")
        models.RecurringExpense.objects.create(
            store=self.store, category=self.rent, amount=Decimal("310"), start_month=date(2026, 3, 1)
        )
        with mock.patch("apps.store.finance.today", return_value=d):
            p = finance.pnl(self.store.pk, d, d, period="day")
        L = p["lines"]
        self.assertEqual(L["gross_sales"], "30.00")
        self.assertEqual(L["returns"], "12.00")
        self.assertEqual(L["net_revenue"], "18.00")
        self.assertEqual(L["cogs"], "8.00")  # cost stays: the drink was made
        self.assertEqual(L["gross_profit"], "10.00")
        self.assertEqual(L["opex"], "10.00")
        self.assertEqual(L["net_profit"], "0.00")
        self.assertEqual(p["coverage"]["uncosted_revenue"], "6.00")  # the water

    def test_points_redeemed_is_its_own_line(self):
        d = date(2026, 3, 10)
        points_service.adjust(self.store, self.cust, 50, "هدية", key="g")
        with mock.patch("django.utils.timezone.now", return_value=at(d, 18)):
            self.sell([(self.latte, 1)], customer=self.cust.pk, beans_spent=20)  # ₪2 off
        with mock.patch("apps.store.finance.today", return_value=d):
            L = finance.pnl(self.store.pk, d, d, period="day", with_compare=False)["lines"]
        self.assertEqual(L["points_redeemed"], "2.00")
        self.assertEqual(L["discounts"], "0.00")
        self.assertEqual(L["net_revenue"], "10.00")

    def test_purchases_and_counts_are_never_costs(self):
        d = date(2026, 3, 10)
        milk = models.InventoryItem.objects.create(
            store=self.store, name="حليب", purchase_qty=1, purchase_unit="l", purchase_cost=5
        )
        from apps.store.cafe_api import _apply_move

        with mock.patch("django.utils.timezone.now", return_value=at(d, 12)):
            _apply_move(milk, "purchase", Decimal("10000"), self.owner, total_cost=Decimal("50"))
            _apply_move(milk, "count", Decimal("-4000"), self.owner)
            _apply_move(milk, "waste", Decimal("-1000"), self.owner)
        with mock.patch("apps.store.finance.today", return_value=d):
            p = finance.pnl(self.store.pk, d, d, period="day", with_compare=False)
        self.assertEqual(p["lines"]["waste"], "5.00")
        self.assertEqual(p["memo"]["purchases"], "50.00")
        self.assertEqual(p["lines"]["gross_profit"], "-5.00")

    def test_a_night_sale_belongs_to_the_day_before(self):
        d = date(2026, 3, 10)
        with mock.patch("django.utils.timezone.now", return_value=at(d + timedelta(days=1), 1, 30)):
            self.sell([(self.latte, 1)])
        with mock.patch("apps.store.finance.today", return_value=d + timedelta(days=1)):
            self.assertEqual(finance.pnl(self.store.pk, d, d, with_compare=False)["lines"]["net_revenue"], "12.00")

    def test_shift_filter_crosses_midnight(self):
        d = date(2026, 3, 10)
        evening = models.Shift.objects.create(
            store=self.store, name="مسائي", start=time(19), end=time(2), wage_per_day=Decimal("100")
        )
        for hh, day in ((14, d), (20, d), (1, d + timedelta(days=1))):
            with mock.patch("django.utils.timezone.now", return_value=at(day, hh)):
                self.sell([(self.latte, 1)])
        with mock.patch("apps.store.finance.today", return_value=d):
            p = finance.pnl(self.store.pk, d, d, shift=evening, with_compare=False)
        self.assertEqual(p["lines"]["net_revenue"], "24.00")
        self.assertEqual(p["lines"]["contribution"], str(Decimal("24") - Decimal("8") - Decimal("100")) + ".00")
        self.assertNotIn("net_profit", p["lines"], "a shift shows contribution, never net profit")


# ── who sees money ──────────────────────────────────────────────────────────
class OwnerOnlyTests(Base):
    def test_employee_is_refused_every_money_endpoint(self):
        for url in ("/api/v1/reports/pnl/", "/api/v1/reports/cafe/", "/api/v1/reports/hours/",
                    "/api/v1/sales/stats/", "/api/v1/sales/day_summary/", "/api/v1/expenses/",
                    "/api/v1/recurring-expenses/", "/api/v1/expense-categories/"):
            self.assertEqual(self.staff.get(url).status_code, 403, url)

    def test_owner_reaches_them(self):
        for url in ("/api/v1/reports/pnl/?period=month", "/api/v1/reports/hours/?period=week",
                    "/api/v1/expenses/month/", "/api/v1/expense-categories/"):
            self.assertEqual(self.api.get(url).status_code, 200, url)

    def test_employee_never_sees_cost(self):
        row = self.staff.get(f"/api/v1/products/{self.latte.pk}/").json()
        self.assertNotIn("cost", row)
        self.assertIn("cost", self.api.get(f"/api/v1/products/{self.latte.pk}/").json())

    def test_employee_saving_a_drink_cannot_change_its_cost(self):
        r = self.staff.patch(f"/api/v1/products/{self.latte.pk}/", {"cost": "0", "price": "13"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.latte.refresh_from_db()
        self.assertEqual(self.latte.cost, Decimal("4"))
        self.assertEqual(self.latte.price, Decimal("13"))

    def test_employee_invoice_lines_carry_no_cost(self):
        sale = self.sell([(self.latte, 1)], client=self.staff)
        data = self.staff.get(f"/api/v1/sales/{sale.pk}/").json()
        self.assertNotIn("unit_cost", data["items"][0])


# ── offline: a customer made at the counter, then a sale for them ───────────
class OfflineCustomerLinkTests(Base):
    def test_sale_finds_the_customer_by_its_client_id(self):
        r = self.staff.post("/api/v1/customers/", {"name": "زبون جديد", "phone": "0591", "client_uuid": "c-1"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        sale = self.sell([(self.latte, 1)], client=self.staff, customer_client_uuid="c-1")
        self.assertEqual(sale.customer.name, "زبون جديد")

    def test_unknown_client_id_is_a_clear_error(self):
        r = self.staff.post("/api/v1/sales/", {"items": [{"product": self.latte.pk}],
                                                "customer_client_uuid": "nope"}, format="json")
        self.assertEqual(r.status_code, 400)


class OfflineCustomerMergeTests(Base):
    def test_retry_of_the_same_create_is_not_a_duplicate(self):
        body = {"name": "جديد", "phone": "0592", "client_uuid": "c-9"}
        self.assertEqual(self.staff.post("/api/v1/customers/", body, format="json").status_code, 201)
        r = self.staff.post("/api/v1/customers/", body, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        self.assertEqual(models.Customer.objects.for_pharmacy(self.store).filter(phone="0592").count(), 1)

    def test_offline_customer_with_a_known_phone_adopts_the_existing_one(self):
        r = self.staff.post("/api/v1/customers/", {"name": "سامر م", "phone": "0599", "client_uuid": "c-7",
                                                   "merge_on_phone": True}, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        self.assertEqual(r.json()["id"], self.cust.pk)
        sale = self.sell([(self.latte, 1)], client=self.staff, customer_client_uuid="c-7")
        self.assertEqual(sale.customer_id, self.cust.pk)

    def test_online_duplicate_phone_is_still_refused(self):
        r = self.staff.post("/api/v1/customers/", {"name": "x", "phone": "0599"}, format="json")
        self.assertEqual(r.status_code, 400)


class StatsFollowReturnsTests(Base):
    def test_todays_takings_lose_what_was_refunded(self):
        from django.core.cache import cache

        sale = self.sell([(self.latte, 2)])
        cache.clear()
        before = self.api.get("/api/v1/sales/stats/").json()["periods"]["today"]["amount"]
        self.api.post(f"/api/v1/sales/{sale.pk}/returns/",
                      {"sale_item": sale.items.get().pk, "quantity": "1", "reason": "taste"}, format="json")
        cache.clear()
        after = self.api.get("/api/v1/sales/stats/").json()["periods"]["today"]["amount"]
        self.assertEqual(Decimal(str(before)) - Decimal(str(after)), Decimal("12.00"))


class ReportTabsTests(Base):
    def test_every_tab_is_owner_only(self):
        sale = self.sell([(self.latte, 1)])
        urls = ["/api/v1/reports/items/", f"/api/v1/reports/items/{self.latte.pk}/",
                "/api/v1/reports/times/", "/api/v1/reports/shifts/",
                "/api/v1/reports/customers/", "/api/v1/reports/returns/"]
        for u in urls:
            self.assertEqual(self.api.get(u + "?period=month").status_code, 200, u)
            self.assertEqual(self.staff.get(u + "?period=month").status_code, 403, u)
        self.assertTrue(sale)

    def test_items_rank_and_profit(self):
        self.sell([(self.latte, 3), (self.water, 2)])
        data = self.api.get("/api/v1/reports/items/?period=month").json()
        latte = next(i for i in data["items"] if i["product_id"] == self.latte.pk)
        water = next(i for i in data["items"] if i["product_id"] == self.water.pk)
        self.assertEqual(latte["qty"], "3.00")
        self.assertEqual(latte["profit"], "24.00")  # (12 - 4) × 3
        self.assertEqual(latte["margin_pct"], "66.67")
        # No cost entered: profit is unknown, not 100%.
        self.assertIsNone(water["margin_pct"])
        self.assertEqual(water["profit"], "0.00")
        self.assertEqual(data["items"][0]["product_id"], self.latte.pk)

    def test_item_detail_counts_today(self):
        self.sell([(self.latte, 2)], customer=self.cust.pk)
        d = self.api.get(f"/api/v1/reports/items/{self.latte.pk}/?period=month").json()
        self.assertEqual(d["cups"]["today"], "2.00")
        self.assertEqual(d["period"]["rank"], 1)
        self.assertEqual(d["buyers"][0]["name"], "سامر")
        self.assertIsNotNone(d["last_sold_at"])

    def test_returns_tab_counts_reasons(self):
        sale = self.sell([(self.latte, 2)])
        self.api.post(f"/api/v1/sales/{sale.pk}/returns/",
                      {"sale_item": sale.items.get().pk, "quantity": "1", "reason": "late", "refund": "none"},
                      format="json")
        d = self.api.get("/api/v1/reports/returns/?period=month").json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["remakes"], 1)
        self.assertEqual(d["by_reason"][0]["label"], "تأخّر")
        self.assertEqual(d["rate_pct"], "50.00")

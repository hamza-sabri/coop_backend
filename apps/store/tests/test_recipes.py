"""Recipes and stock: every number here is one somebody will check by hand.

The fixture is a latte: 1 cup, 200 ml milk, 18 g beans. A large one has its
own recipe with 300 ml. Prices: cups 100 for ₪10, milk ₪5 a litre, beans
₪95 a kilo — so one latte costs 0.10 + 1.00 + 1.71 = ₪2.81, exactly.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.store import breakdowns, finance, models, recipes
from apps.store.fulfil import sale_for_order, void_sale_for_order

D = Decimal


class RecipeBase(TestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="كوب", slug="koup")
        self.owner = User.objects.create_user(username="o", password="x", store=self.store)
        self.emp = User.objects.create_user(username="e", password="x", store=self.store, role="employee")
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.staff = APIClient()
        self.staff.force_authenticate(self.emp)

        def item(name, unit, qty, cost, stock):
            i = models.InventoryItem.objects.create(
                store=self.store, name=name, purchase_unit=unit, purchase_qty=D(qty), purchase_cost=D(cost)
            )
            models.InventoryItem.objects.for_pharmacy(self.store).filter(pk=i.pk).update(stock=D(stock))
            models.StockMove.objects.create(store=self.store, item=i, kind="adjust", quantity=D(stock), stock_after=D(stock))
            i.refresh_from_db()
            return i

        self.cup = item("كوب", "piece", "100", "10", "500")
        self.milk = item("حليب", "l", "1", "5", "10000")
        self.beans = item("بن", "kg", "1", "95", "2000")
        # No typed cost: the recipe stands in for it (see TypedCostTests).
        self.latte = models.Product.objects.create(store=self.store, name="لاتيه", price=D("12"))
        self.large = models.ProductVariant.objects.create(product=self.latte, label="كبير", price=D("15"))
        self.small = models.ProductVariant.objects.create(product=self.latte, label="صغير", price=D("10"))
        for i, (it, q) in enumerate(((self.cup, "1"), (self.milk, "200"), (self.beans, "18"))):
            models.RecipeLine.objects.create(store=self.store, product=self.latte, item=it, quantity=D(q), position=i)
        for i, (it, q) in enumerate(((self.cup, "1"), (self.milk, "300"), (self.beans, "18"))):
            models.RecipeLine.objects.create(
                store=self.store, product=self.latte, variant=self.large, item=it, quantity=D(q), position=i
            )

    def stock(self, i):
        i.refresh_from_db()
        return i.stock

    def sell(self, qty=1, variant=None, client=None, **extra):
        line = {"product": self.latte.pk, "quantity": qty}
        if variant:
            line["variant"] = variant.pk
        r = (client or self.api).post("/api/v1/sales/", {"items": [line], **extra}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return models.Sale.objects.for_pharmacy(self.store).get(pk=r.json()["id"])


class ConsumptionTests(RecipeBase):
    def test_a_sale_takes_the_recipe_times_quantity_exactly(self):
        self.sell(2)
        self.assertEqual(self.stock(self.cup), D("498"))
        self.assertEqual(self.stock(self.milk), D("9600"))
        self.assertEqual(self.stock(self.beans), D("1964"))

    def test_the_line_cost_is_the_recipe_cost(self):
        sale = self.sell(1)
        self.assertEqual(sale.items.get().unit_cost, D("2.8100"))

    def test_a_size_with_its_own_recipe_uses_only_that(self):
        sale = self.sell(1, variant=self.large)
        self.assertEqual(self.stock(self.milk), D("9700"))
        self.assertEqual(sale.items.get().unit_cost, D("3.3100"))

    def test_a_size_without_one_uses_the_drinks(self):
        self.sell(1, variant=self.small)
        self.assertEqual(self.stock(self.milk), D("9800"))

    def test_a_drink_without_a_recipe_keeps_its_typed_cost_and_moves_nothing(self):
        tea = models.Product.objects.create(store=self.store, name="شاي", price=D("5"), cost=D("1.5"))
        r = self.api.post("/api/v1/sales/", {"items": [{"product": tea.pk}]}, format="json")
        self.assertEqual(models.SaleItem.objects.for_pharmacy(self.store).get(sale_id=r.json()["id"]).unit_cost, D("1.5"))
        self.assertEqual(self.stock(self.cup), D("500"))

    def test_one_move_per_line_and_ingredient_naming_the_receipt(self):
        sale = self.sell(1)
        moves = models.StockMove.objects.for_pharmacy(self.store).filter(sale=sale)
        self.assertEqual(moves.count(), 3)
        m = moves.get(item=self.milk)
        self.assertEqual((m.kind, m.quantity, m.product_name), ("sale", D("-200"), "لاتيه"))
        self.assertEqual(m.receipt_code, sale.receipt_code)
        self.assertEqual(m.total_cost, D("1.0000"))

    def test_a_resent_offline_sale_takes_once(self):
        self.sell(1, client_uuid="same")
        self.sell(1, client_uuid="same")
        self.assertEqual(self.stock(self.milk), D("9800"))

    def test_voiding_puts_it_back_and_keeps_the_history(self):
        sale = self.sell(3)
        self.assertEqual(self.api.delete(f"/api/v1/sales/{sale.pk}/").status_code, 204)
        self.assertEqual(self.stock(self.milk), D("10000"))
        self.assertEqual(self.stock(self.cup), D("500"))
        # 3 taken + 3 put back, all kept
        self.assertEqual(models.StockMove.objects.for_pharmacy(self.store).filter(kind="sale").count(), 6)

    def test_an_edit_takes_only_the_difference_in_effect(self):
        sale = self.sell(2)
        r = self.api.patch(f"/api/v1/sales/{sale.pk}/", {"items": [{"product": self.latte.pk, "quantity": 3}]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.stock(self.milk), D("9400"))
        net = models.StockMove.objects.for_pharmacy(self.store).filter(sale=sale, item=self.milk)
        self.assertEqual(sum(m.quantity for m in net), D("-600"))

    def test_a_remake_takes_a_second_drink_a_refund_does_not(self):
        sale = self.sell(2)
        line = sale.items.get()
        self.api.post(f"/api/v1/sales/{sale.pk}/returns/", {"sale_item": line.pk, "quantity": "1", "reason": "spilled", "refund": "none"}, format="json")
        self.assertEqual(self.stock(self.milk), D("9400"))
        self.api.post(f"/api/v1/sales/{sale.pk}/returns/", {"sale_item": line.pk, "quantity": "1", "reason": "taste"}, format="json")
        self.assertEqual(self.stock(self.milk), D("9400"))

    def test_stock_may_go_negative_the_till_never_blocks(self):
        models.InventoryItem.objects.for_pharmacy(self.store).filter(pk=self.cup.pk).update(stock=D("1"))
        self.sell(3)
        self.assertEqual(self.stock(self.cup), D("-2"))

    def test_app_order_collect_and_uncollect(self):
        cust = models.Customer.objects.create(store=self.store, name="س")
        order = models.Order.unguarded.create(store=self.store, customer=cust, total=D("24"))
        models.OrderItem.unguarded.create(order=order, product=self.latte, name="لاتيه", unit_price=D("12"), quantity=D("2"))
        sale_for_order(order)
        self.assertEqual(self.stock(self.milk), D("9600"))
        order.refresh_from_db()
        void_sale_for_order(order)
        self.assertEqual(self.stock(self.milk), D("10000"))


class LedgerTests(RecipeBase):
    def test_opening_plus_moves_equals_closing_and_matches_the_shelf(self):
        self.sell(2)
        self.sell(1, variant=self.large)
        self.api.post(f"/api/v1/inventory-items/{self.milk.pk}/waste/", {"quantity": "0.5", "unit": "l"}, format="json")
        self.api.post(f"/api/v1/inventory-items/{self.milk.pk}/purchase/", {"quantity": "12", "unit": "l", "cost": "60"}, format="json")
        today = finance.today()
        self.milk.refresh_from_db()
        led = breakdowns.item_ledger(self.milk, today.replace(day=1), today, owner=True)
        total = D(led["opening"]) + sum(D(r["quantity"]) for r in led["rows"])
        self.assertEqual(total, D(led["closing"]))
        self.assertEqual(D(led["closing"]), self.stock(self.milk))
        self.assertTrue(led["consistent"])
        self.assertEqual(D(led["closing"]), D("10000") - 400 - 300 - 500 + 12000)
        used = {u["name"]: D(u["quantity"]) for u in led["used_by"]}
        self.assertEqual(used["لاتيه — كبير"], D("300"))
        self.assertEqual(used["لاتيه"], D("400"))

    def test_employee_sees_quantities_not_money(self):
        self.sell(1)
        d = self.staff.get(f"/api/v1/inventory-items/{self.milk.pk}/ledger/?period=month").json()
        self.assertTrue(d["consistent"])
        self.assertNotIn("cost", d["rows"][0])
        self.assertNotIn("total_cost", d["moves"][0])


class PnlRecipeTests(RecipeBase):
    def test_a_count_shortfall_on_a_recipe_item_is_a_loss(self):
        d = date(2026, 3, 10)
        at = finance.bounds(d, d)[0].replace(hour=15)
        from apps.store.cafe_api import _apply_move

        with mock.patch("django.utils.timezone.now", return_value=at):
            _apply_move(self.milk, "count", D("-1000"), self.owner)  # a litre missing
            _apply_move(self.milk, "adjust", D("5000"), self.owner)  # an opening balance: not P&L
        with mock.patch("apps.store.finance.today", return_value=d):
            L = finance.pnl(self.store.pk, d, d, with_compare=False)["lines"]
        self.assertEqual(L["count_shortfall"], "5.00")
        self.assertEqual(L["gross_profit"], "-5.00")

    def test_a_remake_costs_a_second_drink(self):
        d = finance.today()
        sale = self.sell(1)
        self.api.post(f"/api/v1/sales/{sale.pk}/returns/", {"sale_item": sale.items.get().pk, "reason": "spilled", "refund": "none"}, format="json")
        L = finance.pnl(self.store.pk, d, d, with_compare=False)["lines"]
        self.assertEqual(L["cogs"], "2.81")
        self.assertEqual(L["remakes"], "2.81")
        self.assertEqual(L["gross_profit"], str(D("12") - D("5.62")))


class RecipeApiTests(RecipeBase):
    def test_put_converts_units_and_prices_the_cup(self):
        r = self.api.put(f"/api/v1/products/{self.latte.pk}/recipe/", {"variant": None, "lines": [
            {"item": self.cup.pk, "quantity": "1", "unit": "piece"},
            {"item": self.milk.pk, "quantity": "0.25", "unit": "l"},
            {"item": self.beans.pk, "quantity": "18", "unit": "g"},
        ]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["base"][1]["quantity"], "250.000")
        self.assertEqual(r.json()["base_cost"], "3.0600")

    def test_refuses_wrong_units_duplicates_and_employees(self):
        bad_unit = {"variant": None, "lines": [{"item": self.milk.pk, "quantity": "1", "unit": "kg"}]}
        self.assertEqual(self.api.put(f"/api/v1/products/{self.latte.pk}/recipe/", bad_unit, format="json").status_code, 400)
        dup = {"variant": None, "lines": [{"item": self.cup.pk, "quantity": "1"}, {"item": self.cup.pk, "quantity": "1"}]}
        self.assertEqual(self.api.put(f"/api/v1/products/{self.latte.pk}/recipe/", dup, format="json").status_code, 400)
        self.assertEqual(self.staff.get(f"/api/v1/products/{self.latte.pk}/recipe/").status_code, 403)

    def test_empty_size_recipe_falls_back_to_the_drink(self):
        self.api.put(f"/api/v1/products/{self.latte.pk}/recipe/", {"variant": self.large.pk, "lines": []}, format="json")
        self.sell(1, variant=self.large)
        self.assertEqual(self.stock(self.milk), D("9800"))

    def test_versions_save_together_or_not_at_all(self):
        url = f"/api/v1/products/{self.latte.pk}/recipe/"
        before = list(models.RecipeLine.objects.unscoped().filter(product=self.latte).values_list("item_id", "variant_id", "quantity"))
        bad = {"versions": [
            {"variant": None, "lines": [{"item": self.cup.pk, "quantity": "1", "unit": "piece"}]},
            {"variant": self.large.pk, "lines": [{"item": self.milk.pk, "quantity": "0", "unit": "ml"}]},
        ]}
        r = self.api.put(url, bad, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn(self.large.label, str(r.json()))
        after = list(models.RecipeLine.objects.unscoped().filter(product=self.latte).values_list("item_id", "variant_id", "quantity"))
        self.assertEqual(before, after)  # the good base was not written either

        ok = {"versions": [
            {"variant": None, "lines": [{"item": self.cup.pk, "quantity": "1", "unit": "piece"}, {"item": self.milk.pk, "quantity": "200", "unit": "ml"}]},
            {"variant": self.large.pk, "lines": [{"item": self.cup.pk, "quantity": "1", "unit": "piece"}, {"item": self.milk.pk, "quantity": "0.3", "unit": "l"}]},
        ]}
        r = self.api.put(url, ok, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        large = next(v for v in r.json()["variants"] if v["id"] == self.large.pk)
        self.assertTrue(large["own"])
        self.assertEqual(large["lines"][1]["quantity"], "300.000")
        self.sell(2, variant=self.large)
        self.assertEqual(self.stock(self.milk), D("10000") - D("600"))

    def test_versions_refuse_another_drinks_size(self):
        other = models.Product.objects.unscoped().exclude(pk=self.latte.pk).first()
        if other is None:
            return
        r = self.api.put(f"/api/v1/products/{other.pk}/recipe/", {"versions": [{"variant": self.large.pk, "lines": []}]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_an_ingredient_in_a_recipe_cannot_be_deleted(self):
        r = self.api.delete(f"/api/v1/inventory-items/{self.milk.pk}/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("لاتيه", r.json()["detail"])


class InventoryCategoryTests(RecipeBase):
    def test_defaults_then_rename_carries_to_items(self):
        models.InventoryItem.objects.for_pharmacy(self.store).filter(pk=self.milk.pk).update(category="ألبان")
        cats = self.api.get("/api/v1/inventory-categories/").json()
        names = [c["name"] for c in cats]
        self.assertIn("ألبان", names)
        self.assertIn("تغليف", names)
        dairy = next(c for c in cats if c["name"] == "ألبان")
        self.api.patch(f"/api/v1/inventory-categories/{dairy['id']}/", {"name": "حليب ومشتقاته"}, format="json")
        self.milk.refresh_from_db()
        self.assertEqual(self.milk.category, "حليب ومشتقاته")
        self.assertEqual(self.api.delete(f"/api/v1/inventory-categories/{dairy['id']}/").status_code, 400)

    def test_employees_read_but_do_not_change(self):
        self.assertEqual(self.staff.get("/api/v1/inventory-categories/").status_code, 200)
        self.assertEqual(self.staff.post("/api/v1/inventory-categories/", {"name": "x"}, format="json").status_code, 403)


class InventoryInsightTests(RecipeBase):
    def test_days_left_reads_the_last_fortnight_of_use(self):
        self.sell(10)  # 2,000 ml of milk today; the item is one day old
        body = self.api.get("/api/v1/inventory-items/").json()
        rows = {r["id"]: r for r in (body.get("results", body) if isinstance(body, dict) else body)}
        milk = rows[self.milk.pk]
        self.assertEqual(milk["daily_use"], "2000.000")
        self.assertEqual(milk["days_left"], 4)  # 8,000 ml left ÷ 2,000 a day
        self.assertEqual(rows[self.cup.pk]["days_left"], 49)  # 490 cups ÷ 10 a day
        # employees see quantities, not money
        body = self.staff.get("/api/v1/inventory-items/").json()
        mine = (body.get("results", body) if isinstance(body, dict) else body)[0]
        self.assertIn("days_left", mine)
        self.assertNotIn("unit_cost", mine)

    def test_insights_reconcile_with_the_moves(self):
        self.sell(3)
        sale = self.sell(1)
        self.api.post(f"/api/v1/sales/{sale.pk}/returns/", {"sale_item": sale.items.get().pk, "reason": "spilled", "refund": "none"}, format="json")
        r = self.api.get("/api/v1/inventory-items/insights/?period=day")
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()["data"] if "data" in r.json() else r.json()
        self.assertEqual(data["flow"]["used"], "11.24")     # 4 lattes × 2.81
        self.assertEqual(data["flow"]["remakes"], "2.81")
        self.assertEqual(data["daily"][0]["used"], "14.05")  # sales + remake
        self.assertEqual(data["top_used"][0]["name"], "بن")  # 5 × 1.71
        self.assertEqual(self.staff.get("/api/v1/inventory-items/insights/").status_code, 403)


class CustomerProfileTests(RecipeBase):
    def test_profile_counts_visits_favourites_and_hides_money_from_staff(self):
        c = models.Customer.objects.create(store=self.store, name="سارة", phone="0590000001")
        for _ in range(3):
            self.sell(1, customer=c.pk)
        self.sell(2, variant=self.large, customer=c.pk)
        r = self.api.get(f"/api/v1/customers/{c.pk}/profile/")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json().get("data", r.json())
        self.assertEqual(d["visits"], 4)
        self.assertEqual(d["cups"], "5")
        self.assertEqual(d["favourites"][0]["name"], "لاتيه")
        self.assertEqual(d["favourites"][0]["share"], "100.00")
        self.assertEqual(d["spent"], "66.00")  # 3 × 12 + 2 × 15
        self.assertEqual(d["status"], "new")
        self.assertEqual(sum(w["visits"] for w in d["weekly"]), 4)
        s = self.staff.get(f"/api/v1/customers/{c.pk}/profile/")
        sd = s.json().get("data", s.json())
        self.assertNotIn("spent", sd)
        self.assertNotIn("spend", sd["weekly"][0])


class TypedCostTests(RecipeBase):
    def test_the_owners_cost_wins_over_the_ingredients(self):
        models.Product.objects.for_pharmacy(self.store).filter(pk=self.latte.pk).update(cost=D("4.50"))
        sale = self.sell(1)
        self.assertEqual(sale.items.get().unit_cost, D("4.5000"))
        self.assertEqual(self.stock(self.milk), D("9800"))  # the shelf still moves by the recipe

    def test_a_sizes_own_cost_wins_too(self):
        models.ProductVariant.objects.for_pharmacy(self.store).filter(pk=self.large.pk).update(cost=D("5"))
        sale = self.sell(1, variant=self.large)
        self.assertEqual(sale.items.get().unit_cost, D("5.0000"))


class StoredAvatarTests(RecipeBase):
    def test_pos_list_and_sales_serve_a_link_not_the_storage_key(self):
        from unittest import mock

        c = models.Customer.objects.create(store=self.store, name="ليان", phone="0590000009", avatar="b2://avatars/x.jpg")
        self.sell(1, customer=c.pk)
        with mock.patch("apps.core.uploads.default_storage.url", return_value="https://signed.example/x.jpg"):
            q = self.api.get("/api/v1/customers/quick/").json()
            q = q.get("data", q)
            row = next(r for r in q["results"] if r["id"] == c.pk)
            self.assertEqual(row["avatar"], "https://signed.example/x.jpg")
            s = self.api.get("/api/v1/sales/").json()
            s = s.get("data", s)
            rows = s["results"] if isinstance(s, dict) else s
            self.assertEqual(rows[0]["customer_avatar"], "https://signed.example/x.jpg")


class StaffAccessTests(RecipeBase):
    def test_owner_sees_staff_but_not_superusers_and_superuser_is_owner(self):
        su = User.objects.create_user(username="root@x.com", password="x", store=self.store, role="employee",
                                      is_superuser=True, is_staff=True)
        body = self.api.get("/api/v1/staff/").json()
        body = body.get("data", body)
        names = [u["username"] for u in (body.get("results", body) if isinstance(body, dict) else body)]
        self.assertIn("e", names)
        self.assertNotIn("root@x.com", names)
        root = APIClient()
        root.force_authenticate(su)
        me = root.get("/api/v1/auth/me/").json()
        me = me.get("data", me)
        self.assertTrue(me["is_owner"])
        self.assertEqual(su.staff_name, "الإدارة")
        self.assertEqual(self.staff.get("/api/v1/staff/").status_code, 403)


class ExpenseKindTests(RecipeBase):
    def setUp(self):
        super().setUp()
        finance.ensure_default_categories(self.store.pk)
        self.cats = {c.key: c for c in models.ExpenseCategory.objects.for_pharmacy(self.store)}

    def test_a_salary_names_its_employee(self):
        url = "/api/v1/recurring-expenses/"
        r = self.api.post(url, {"category": self.cats["salaries"].pk, "amount": "1800", "start_month": "2026-09-01"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("staff", str(r.json()))
        r = self.api.post(url, {"category": self.cats["salaries"].pk, "amount": "1800", "start_month": "2026-09-01",
                                "staff": self.emp.pk}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json().get("data", r.json())
        self.assertEqual(body["staff_name"], self.emp.staff_name)
        self.assertEqual(body["category_key"], "salaries")

    def test_no_staff_from_another_shop_or_the_platform(self):
        other = models.Store.objects.create(name="X", slug="x")
        outsider = User.objects.create_user(username="out", password="x", store=other, role="employee")
        root = User.objects.create_user(username="root2", password="x", store=self.store, is_superuser=True)
        for who in (outsider, root):
            r = self.api.post("/api/v1/expenses/", {"category": self.cats["salaries"].pk, "amount": "100",
                                                    "period": "2026-09-01", "staff": who.pk}, format="json")
            self.assertEqual(r.status_code, 400)

    def test_a_bill_keeps_its_payee_and_drops_a_stray_staff(self):
        r = self.api.post("/api/v1/expenses/", {"category": self.cats["maintenance"].pk, "amount": "250",
                                                "period": "2026-09-01", "note": "تصليح ماكينة القهوة",
                                                "payee": "أبو سامر", "staff": self.emp.pk}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json().get("data", r.json())
        self.assertEqual(body["payee"], "أبو سامر")
        self.assertIsNone(body["staff"])

    def test_month_view_has_a_six_month_trend(self):
        r = self.api.get("/api/v1/expenses/month/?month=2026-09")
        d = r.json().get("data", r.json())
        self.assertEqual(len(d["trend"]), 6)
        self.assertEqual(d["trend"][-1]["month"], "2026-09")


class SalesSummaryTests(RecipeBase):
    def test_summary_counts_the_filtered_list_and_hides_money_from_staff(self):
        self.sell(1)
        self.sell(2)
        day = finance.today().isoformat()
        r = self.api.get(f"/api/v1/sales/summary/?day_from={day}&day_to={day}")
        d = r.json().get("data", r.json())
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["total"], "36.00")
        s = self.staff.get(f"/api/v1/sales/summary/?day_from={day}&day_to={day}").json()
        s = s.get("data", s)
        self.assertEqual(s["count"], 2)
        self.assertNotIn("total", s)
        # yesterday is empty
        y = (finance.today() - __import__("datetime").timedelta(days=1)).isoformat()
        r = self.api.get(f"/api/v1/sales/summary/?day_from={y}&day_to={y}").json()
        self.assertEqual(r.get("data", r)["count"], 0)


class CustomerTableTests(RecipeBase):
    def test_table_lists_visits_and_hides_spend_from_staff(self):
        c = models.Customer.objects.create(store=self.store, name="دانا", phone="0590000002")
        models.Customer.objects.create(store=self.store, name="بلا زيارات", phone="0590000003")
        self.sell(1, customer=c.pk)
        self.sell(1, customer=c.pk)
        d = self.api.get("/api/v1/customers/table/").json()
        d = d.get("data", d)
        row = next(r for r in d["results"] if r["id"] == c.pk)
        self.assertEqual(row["visits"], 2)
        self.assertEqual(row["spent"], "24.00")
        self.assertEqual(row["status"], "new")
        self.assertEqual(row["days_since"], 0)
        none = next(r for r in d["results"] if r["name"] == "بلا زيارات")
        self.assertEqual(none["status"], "no_visits")
        s = self.staff.get("/api/v1/customers/table/").json()
        s = s.get("data", s)
        self.assertNotIn("spent", s["results"][0])


class StaffBoardTests(RecipeBase):
    def test_board_has_month_sales_and_the_salary_from_expenses(self):
        finance.ensure_default_categories(self.store.pk)
        salaries = models.ExpenseCategory.objects.for_pharmacy(self.store).get(key="salaries")
        month = finance.today().replace(day=1)
        models.RecurringExpense.objects.create(
            store=self.store, category=salaries, amount=D("1800"), staff=self.emp, start_month=month
        )
        self.sell(2, client=self.staff)
        root = User.objects.create_user(username="root3", password="x", store=self.store, is_superuser=True)
        d = self.api.get("/api/v1/staff/board/").json()
        d = d.get("data", d)
        rows = {r["username"]: r for r in d["results"]}
        self.assertNotIn(root.username, rows)
        self.assertEqual(rows["e"]["month_sales"], 1)
        self.assertEqual(rows["e"]["month_total"], "24.00")
        self.assertEqual(rows["e"]["salary"], "1800.00")
        self.assertIsNotNone(rows["e"]["last_sale"])
        self.assertIsNone(rows["o"]["salary"])
        self.assertEqual(self.staff.get("/api/v1/staff/board/").status_code, 403)


class CustomersReportAvatarTests(RecipeBase):
    def test_top_customers_carry_a_servable_photo(self):
        c = models.Customer.objects.create(store=self.store, name="ريم", phone="0590000009", avatar="b2://faces/r.jpg")
        self.sell(1, customer=c.pk)
        with mock.patch("apps.store.breakdowns.resolve_stored_url", side_effect=lambda v: f"https://signed/{v[5:]}" if v else ""):
            today = finance.today()
            d = breakdowns.customers_report(self.store.pk, today, today)
        self.assertEqual(d["top"][0]["avatar"], "https://signed/faces/r.jpg")


class StaffDeleteAndPagesTests(RecipeBase):
    def test_delete_only_without_invoices_and_never_superusers_listed(self):
        temp = User.objects.create_user(username="temp", password="x", store=self.store, role="employee")
        self.assertEqual(self.api.delete(f"/api/v1/staff/{temp.pk}/").status_code, 204)
        self.assertFalse(User.objects.filter(pk=temp.pk).exists())
        self.sell(1, client=self.staff)  # the employee now has an invoice
        r = self.api.delete(f"/api/v1/staff/{self.emp.pk}/")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(User.objects.filter(pk=self.emp.pk).exists())
        self.assertEqual(self.api.delete(f"/api/v1/staff/{self.owner.pk}/").status_code, 400)
        root = User.objects.create_user(username="root9", password="x", store=self.store, is_superuser=True)
        su = APIClient()
        su.force_authenticate(root)
        body = su.get("/api/v1/staff/").json()
        body = body.get("data", body)
        rows = body.get("results", body) if isinstance(body, dict) else body
        self.assertNotIn("root9", [u["username"] for u in rows])

    def test_menu_and_stock_are_separate_switches(self):
        self.emp.allowed_modules = ["pos"]
        self.emp.save()
        # A POS-only cashier still lists the menu and categories to sell from…
        self.assertEqual(self.staff.get("/api/v1/products/").status_code, 200)
        self.assertEqual(self.staff.get("/api/v1/categories/").status_code, 200)
        # …but cannot change a drink or open the stock.
        self.assertEqual(self.staff.patch(f"/api/v1/products/{self.latte.pk}/", {"price": "1"}, format="json").status_code, 403)
        self.assertEqual(self.staff.get("/api/v1/inventory-items/").status_code, 403)
        self.emp.allowed_modules = ["pos", "stock"]
        self.emp.save()
        self.assertEqual(self.staff.get("/api/v1/inventory-items/").status_code, 200)


class SupplierTests(RecipeBase):
    """Suppliers are a list the stock item picks from; the item keeps the name."""

    def rows(self, r):
        body = r.json()
        body = body.get("data", body) if isinstance(body, dict) else body
        return body.get("results", body) if isinstance(body, dict) else body

    def test_create_pick_rename_delete(self):
        r = self.api.post("/api/v1/suppliers/", {"name": " ألبان الجنيدي ", "phone": "0599"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        sid = self.rows(r)["id"]
        # Same name again → the same row, not a duplicate.
        self.api.post("/api/v1/suppliers/", {"name": "ألبان الجنيدي"}, format="json")
        self.assertEqual(models.Supplier.objects.for_pharmacy(self.store).count(), 1)

        item = models.InventoryItem.objects.create(store=self.store, name="حليب", purchase_unit="l")
        self.assertEqual(self.api.patch(f"/api/v1/inventory-items/{item.pk}/", {"supplier": "ألبان الجنيدي"},
                                        format="json").status_code, 200)
        listed = self.rows(self.api.get("/api/v1/suppliers/"))
        self.assertEqual(listed[0]["items"], 1)

        self.api.patch(f"/api/v1/suppliers/{sid}/", {"name": "الجنيدي"}, format="json")
        item.refresh_from_db()
        self.assertEqual(item.supplier, "الجنيدي", "a rename follows onto the items")

        self.assertEqual(self.api.delete(f"/api/v1/suppliers/{sid}/").status_code, 204)
        item.refresh_from_db()
        self.assertEqual(item.supplier, "")

    def test_typed_supplier_joins_the_list_and_employees_only_read(self):
        item = models.InventoryItem.objects.create(store=self.store, name="بن", purchase_unit="kg")
        self.api.patch(f"/api/v1/inventory-items/{item.pk}/", {"supplier": "محمصة النورس"}, format="json")
        self.assertTrue(models.Supplier.objects.for_pharmacy(self.store).filter(name="محمصة النورس").exists())
        self.assertEqual(self.staff.get("/api/v1/suppliers/").status_code, 200)
        self.assertEqual(self.staff.post("/api/v1/suppliers/", {"name": "x"}, format="json").status_code, 403)


class ArabicSearchTests(RecipeBase):
    """أ = إ = آ = ا, ة = ه, ى = ي in every server search, like the app's boxes."""

    def rows(self, url):
        r = self.api.get(url)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        body = body.get("data", body) if isinstance(body, dict) else body
        return body.get("results", body) if isinstance(body, dict) else body

    def test_pattern(self):
        import re

        from apps.core.search import arabic_pattern

        p = arabic_pattern("اسبريسو")
        for name in ("إسبريسو", "أسبريسو", "آسبريسو", "اسبريسو مزدوج", "إِسبريسو"):
            self.assertTrue(re.search(p, name), name)
        self.assertFalse(re.search(arabic_pattern("قهوه"), "قهوى"))
        self.assertTrue(re.search(arabic_pattern("قهوه"), "قهوة تركية"))

    def test_customers_products_and_stock(self):
        models.Customer.objects.create(store=self.store, name="أحمد خالد", phone="0591112223")
        models.Product.objects.create(store=self.store, name="إسبريسو", price="8")
        models.InventoryItem.objects.create(store=self.store, name="قهوة إثيوبية", purchase_unit="kg")
        self.assertEqual([c["name"] for c in self.rows("/api/v1/customers/?search=احمد")], ["أحمد خالد"])
        self.assertEqual(len(self.rows("/api/v1/customers/?search=٠٥٩١١")), 1, "Arabic-Indic digits")
        self.assertIn("إسبريسو", [p["name"] for p in self.rows("/api/v1/products/?search=اسبريسو")])
        self.assertEqual([i["name"] for i in self.rows("/api/v1/inventory-items/?search=قهوه اثيوبيه")], ["قهوة إثيوبية"])


class TillCustomerListTests(RecipeBase):
    """A customer who signs up in the app shows at the till straight away."""

    def quick(self):
        body = self.api.get("/api/v1/customers/quick/").json()
        body = body.get("data", body)
        return body["results"]

    def test_app_signup_reaches_the_till_list(self):
        from django.core.cache import cache

        from apps.accounts.firebase import upsert_customer_from_firebase

        cache.clear()
        before = {c["id"] for c in self.quick()}  # caches the list
        with self.captureOnCommitCallbacks(execute=True):
            cust = upsert_customer_from_firebase(self.store, {"sub": "fb-1", "name": "hamza sabri"})
        rows = {c["id"]: c for c in self.quick()}
        self.assertNotIn(cust.pk, before)
        self.assertIn(cust.pk, rows)
        self.assertTrue(rows[cust.pk]["signed_up"])
        self.assertEqual(rows[cust.pk]["beans"], 5)


class OffMenuTests(RecipeBase):
    """A drink switched off (في المنيو) is not offered at the till."""

    def test_switched_off_drink_leaves_the_till(self):
        from django.core.cache import cache

        cache.clear()
        off = models.Product.objects.create(store=self.store, name="test", price="17", is_active=False)
        body = self.api.get("/api/v1/products/pos_catalog/").json()
        ids = [r["id"] for r in body.get("data", body)["results"]]
        self.assertNotIn(off.pk, ids)
        self.assertIn(self.latte.pk, ids)
        body = self.api.get("/api/v1/products/?is_active=true&page_size=100").json()
        body = body.get("data", body)
        names = [r["name"] for r in body.get("results", body)]
        self.assertNotIn("test", names)


class PointsRateTests(RecipeBase):
    """The shop sets how many points make 1 ₪; points are spent in whole
    shekels only; a bill keeps the value its points had when paid."""

    def setUp(self):
        super().setUp()
        from apps.store import points as points_service

        self.cust = models.Customer.objects.create(store=self.store, name="ليان")
        points_service.adjust(self.store, self.cust, 175, "رصيد", key="t-rate")

    def rate(self, n, client=None):
        return (client or self.api).patch("/api/v1/points/rules/", {"points_per_ils": n}, format="json")

    def test_only_fixed_rates_and_only_the_owner(self):
        self.assertEqual(self.rate(55).status_code, 400)
        self.assertEqual(self.rate(50, self.staff).status_code, 403)
        r = self.rate(50)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json().get("data", r.json())
        self.assertEqual(body["points_per_ils"], 50)
        self.assertEqual(body["points_outstanding"], 175)

    def test_spends_whole_shekels_only(self):
        # 10 per ₪: asked 55 → spends 50 = 5 ₪; 12 ₪ latte → pays 7.
        s = self.sell(1, customer=self.cust.pk, beans_spent=55)
        self.assertEqual(s.beans_spent, 50)
        self.assertEqual(s.beans_value, D("5.00"))
        self.assertEqual(s.discounted_total, D("7.00"))
        # 50 per ₪: balance 125 (+earned) → at most 2 ₪ = 100 points.
        self.rate(50)
        s2 = self.sell(1, customer=self.cust.pk, beans_spent=999)
        self.assertEqual(s2.beans_spent % 50, 0)
        self.assertEqual(s2.beans_value, D(s2.beans_spent) / 50)

    def test_a_rate_change_never_reprices_a_paid_bill(self):
        s = self.sell(1, customer=self.cust.pk, beans_spent=30)  # 3 ₪ at 10/₪
        q = f"?period=custom&start={s.created_at.date()}&end={s.created_at.date()}"
        before = self.api.get("/api/v1/reports/pnl/" + q).json()
        self.rate(100)
        after = self.api.get("/api/v1/reports/pnl/" + q).json()
        get = lambda b: b.get("data", b)["lines"]
        self.assertEqual(get(before)["points_redeemed"], "3.00")
        self.assertEqual(get(after)["points_redeemed"], "3.00")
        self.assertEqual(get(after)["net_revenue"], get(before)["net_revenue"])
        detail = self.api.get(f"/api/v1/sales/{s.pk}/").json()
        self.assertEqual(detail.get("data", detail)["beans_value"], "3.00")

    def test_earning_follows_the_rate(self):
        from apps.store import points as points_service

        self.rate(100)
        # 2% of 50 ₪ = 1 ₪ = 100 points at 100/₪ (was 10 at 10/₪).
        self.assertEqual(points_service.points_for(D("50"), store=self.store.pk, rate=D("0.02")), 100)
        self.assertEqual(points_service.value_of(250, self.store.pk), D("2.50"))


class PointsAreCalculatedTests(RecipeBase):
    def test_employee_cannot_edit_points_owner_can(self):
        c = models.Customer.objects.create(store=self.store, name="سما")
        r = self.staff.post(f"/api/v1/customers/{c.pk}/points/", {"delta": 50, "note": "x"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.staff.get(f"/api/v1/customers/{c.pk}/points/").status_code, 200)
        r = self.api.post(f"/api/v1/customers/{c.pk}/points/", {"delta": 50, "note": "x"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_a_customer_needs_only_a_name(self):
        r = self.staff.post("/api/v1/customers/", {"name": "زبون بلا هاتف"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)


class CardPaymentTests(RecipeBase):
    def test_a_card_sale_is_recorded_and_filtered(self):
        s = self.sell(1, payment_method="card")
        self.assertEqual(s.payment_method, "card")
        self.assertIsNone(s.debt_id)
        cash = self.sell(1)
        self.assertEqual(cash.payment_method, "cash")
        body = self.api.get("/api/v1/sales/?payment_method=card").json()
        body = body.get("data", body)
        ids = [r["id"] for r in body.get("results", body)]
        self.assertEqual(ids, [s.pk])


class DrinkBuyersTests(RecipeBase):
    def test_who_orders_a_drink_most(self):
        a = models.Customer.objects.create(store=self.store, name="ريم", avatar="https://x/a.png")
        b = models.Customer.objects.create(store=self.store, name="عمر")
        self.sell(2, customer=a.pk)
        self.sell(1, customer=a.pk)
        self.sell(1, customer=b.pk)
        self.sell(5)  # walk-in: not a buyer
        r = self.api.get(f"/api/v1/reports/items/{self.latte.pk}/?period=month")
        self.assertEqual(r.status_code, 200, r.content)
        buyers = r.json().get("data", r.json())["buyers"]
        self.assertEqual([x["customer_id"] for x in buyers], [a.pk, b.pk])
        self.assertEqual(buyers[0]["qty"], "3.00")
        self.assertEqual(buyers[0]["times"], 2)
        self.assertEqual(buyers[0]["avatar"], "https://x/a.png")


class TillCatalogPictureTests(RecipeBase):
    def test_the_offline_catalogue_carries_the_picture(self):
        from django.core.cache import cache

        cache.clear()
        models.Product.objects.for_pharmacy(self.store).filter(pk=self.latte.pk).update(image="https://img.example/latte.webp")
        for _ in range(2):  # fresh, then from the cache
            body = self.api.get("/api/v1/products/pos_catalog/").json()
            rows = {r["id"]: r for r in body.get("data", body)["results"]}
            self.assertEqual(rows[self.latte.pk]["image"], "https://img.example/latte.webp")

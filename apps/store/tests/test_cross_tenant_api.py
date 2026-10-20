"""Two cafés on one server: café B must be invisible to café A, everywhere.

Café B is filled with rows whose names carry the marker "ZZB" and whose
money carries 777.77. Café A's owner then calls EVERY staff endpoint (lists,
dashboards, reports, exports, actions) and the marker must never appear in a
response; every detail route aimed at a café-B id must miss; every write
aimed at café B must leave it untouched. If a new endpoint is added and
leaks, `test_every_get_endpoint_is_blind_to_the_other_cafe` is the one that
should catch it — add the URL to GET_URLS.

Run: python manage.py test apps.store.tests.test_cross_tenant_api
"""
from datetime import date, time
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.store import models

MARKERS = ("ZZB", "zzb", "777.77", "0599777777")


class TwoCafes(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = models.Store.objects.create(name="مقهى أ", slug="cafe-a")
        cls.b = models.Store.objects.create(name="ZZB مقهى", slug="cafe-b")
        cls.owner_a = User.objects.create_user("owner", password="pa", store=cls.a)
        # Same username in both cafés — usernames are per store.
        cls.owner_b = User.objects.create_user(
            "owner", password="pb", store=cls.b, display_name="ZZB owner"
        )
        cls.emp_b = User.objects.create_user(
            "zzb-cashier", password="x", store=cls.b, role="employee", display_name="ZZB emp"
        )
        # Café A has a little data of its own so lists are not trivially empty.
        cls.cat_a = models.Category.objects.create(store=cls.a, name="ساخن")
        cls.latte_a = models.Product.objects.create(
            store=cls.a, name="لاتيه", price=Decimal("12"), cost=Decimal("4"), category=cls.cat_a
        )
        cls.cust_a = models.Customer.objects.create(store=cls.a, name="سامر", phone="0599000001")

        b = cls.b
        cls.cat_b = models.Category.objects.create(store=b, name="ZZB-cat")
        cls.man_b = models.Manufacturer.objects.create(store=b, name="ZZB-man")
        cls.prod_b = models.Product.objects.create(
            store=b, name="ZZB-latte", price=Decimal("777.77"), cost=Decimal("1"),
            category=cls.cat_b, barcode="7290000000001",
        )
        cls.var_b = models.ProductVariant.objects.create(
            product=cls.prod_b, label="ZZB-big", price=Decimal("777.77")
        )
        cls.img_b = models.ProductImage.objects.create(
            product=cls.prod_b, image="https://example.com/ZZB.jpg"
        )
        cls.cust_b = models.Customer.objects.create(store=b, name="ZZB-cust", phone="0599777777")
        cls.debt_b = models.Debt.objects.create(store=b, customer=cls.cust_b)
        cls.sup_b = models.Supplier.objects.create(store=b, name="ZZB-sup")
        cls.po_b = models.PurchaseOrder.objects.create(store=b)
        cls.invcat_b = models.InventoryCategory.objects.create(store=b, name="ZZB-invcat")
        cls.inv_b = models.InventoryItem.objects.create(store=b, name="ZZB-milk")
        cls.recipe_b = models.RecipeLine.objects.create(
            store=b, product=cls.prod_b, item=cls.inv_b, quantity=Decimal("1")
        )
        cls.expcat_b = models.ExpenseCategory.objects.create(store=b, name="ZZB-expcat")
        month = date.today().replace(day=1)
        cls.exp_b = models.Expense.objects.create(
            store=b, category=cls.expcat_b, amount=Decimal("777.77"), period=month
        )
        cls.rexp_b = models.RecurringExpense.objects.create(
            store=b, category=cls.expcat_b, amount=Decimal("777.77"), start_month=month
        )
        cls.shift_b = models.Shift.objects.create(
            store=b, name="ZZB-shift", start=time(8), end=time(16)
        )
        cls.order_b = models.Order.objects.create(store=b, customer=cls.cust_b)
        models.OrderItem.objects.create(
            order=cls.order_b, name="ZZB-order-line", unit_price=Decimal("777.77")
        )
        models.Notification.objects.create(
            store=b, customer=cls.cust_b, kind="order", title="ZZB-note"
        )
        models.EarnRule.objects.create(store=b, rate_percent=Decimal("7.77"))
        models.AuditLog.objects.create(store=b, action="ZZB-action")

    def setUp(self):
        cache.clear()
        self.A = APIClient()
        self.A.force_authenticate(self.owner_a)
        self.B = APIClient()
        self.B.force_authenticate(self.owner_b)
        # Real activity in café B through its own API: a sale to its customer
        # (points, ledger, reports) and a full cash-drawer day.
        r = self.B.post(
            "/api/v1/sales/",
            {"payment_method": "cash", "customer": self.cust_b.pk,
             "items": [{"product": self.prod_b.pk, "variant": self.var_b.pk, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.sale_b = models.Sale.objects.for_pharmacy(self.b).get(pk=r.json()["id"])
        r = self.B.post("/api/v1/cash-drawer/open/", {"counts": {"100": 7}}, format="json")
        self.assertIn(r.status_code, (200, 201), r.content)
        r = self.B.post(
            "/api/v1/cash-drawer/move/",
            {"direction": "out", "amount": "77.77", "note": "ZZB-move"}, format="json",
        )
        self.assertIn(r.status_code, (200, 201), r.content)
        self.drawer_b = models.CashSession.objects.for_pharmacy(self.b).order_by("-pk").first()
        self.assertIsNotNone(self.drawer_b)

    def assertBlind(self, resp, url):
        self.assertLess(resp.status_code, 500, f"{url} crashed: {resp.content[:300]!r}")
        body = resp.content.decode("utf-8", "replace")
        for m in MARKERS:
            self.assertNotIn(m, body, f"{url} leaked café B data ({m})")


class ReadIsolation(TwoCafes):
    GET_URLS = [
        "/api/v1/auth/me/",
        # router lists + list-level actions
        "/api/v1/products/", "/api/v1/products/catalog_version/",
        "/api/v1/products/export/hesabate/", "/api/v1/products/pos_catalog/",
        "/api/v1/products/stats/", "/api/v1/products/?search=ZZB",
        "/api/v1/products/?barcode=7290000000001",
        "/api/v1/variants/", "/api/v1/categories/", "/api/v1/manufacturers/",
        "/api/v1/customers/", "/api/v1/customers/quick/", "/api/v1/customers/table/",
        "/api/v1/customers/?search=0599777777",
        "/api/v1/debts/", "/api/v1/debts/dashboard/",
        "/api/v1/sales/", "/api/v1/sales/day_summary/", "/api/v1/sales/stats/",
        "/api/v1/sales/summary/",
        "/api/v1/staff/", "/api/v1/staff/board/", "/api/v1/staff/names/",
        "/api/v1/purchase-orders/", "/api/v1/orders/", "/api/v1/orders/live/",
        "/api/v1/inventory-items/", "/api/v1/inventory-items/insights/",
        "/api/v1/inventory-items/stocktake/", "/api/v1/inventory-items/summary/",
        "/api/v1/shifts/", "/api/v1/inventory-categories/", "/api/v1/suppliers/",
        "/api/v1/expense-categories/", "/api/v1/expenses/", "/api/v1/expenses/month/",
        "/api/v1/recurring-expenses/",
        # plain views
        "/api/v1/store/quick-groups/", "/api/v1/pos/cart-state/",
        "/api/v1/cash-drawer/", "/api/v1/cash-drawer/history/",
        "/api/v1/points/rules/", "/api/v1/points/preview/?amount=100",
        "/api/v1/audit/", "/api/v1/store/branding/", "/api/v1/qr/price-page/",
        "/api/v1/reports/cafe/", "/api/v1/reports/pnl/", "/api/v1/reports/hours/",
        "/api/v1/reports/items/", "/api/v1/reports/times/", "/api/v1/reports/shifts/",
        "/api/v1/reports/customers/", "/api/v1/reports/returns/",
        "/api/v1/reports/summary/", "/api/v1/reports/teaser/",
        "/api/v1/reports/sales/summary/", "/api/v1/reports/sales/export/",
        "/api/v1/reports/products/", "/api/v1/reports/filtered-charts/",
        "/api/v1/reports/top-products/", "/api/v1/reports/export/",
        "/api/v1/reports/restock-quota/", "/api/v1/reports/scans/",
    ]

    def test_every_get_endpoint_is_blind_to_the_other_cafe(self):
        for url in self.GET_URLS:
            with self.subTest(url=url):
                self.assertBlind(self.A.get(url), url)

    def test_the_markers_really_are_visible_to_cafe_b(self):
        """Guards the test above: if B cannot see its own rows, A seeing
        nothing proves nothing."""
        for url in ("/api/v1/products/", "/api/v1/customers/", "/api/v1/expenses/",
                    "/api/v1/inventory-items/", "/api/v1/cash-drawer/", "/api/v1/sales/"):
            with self.subTest(url=url):
                body = self.B.get(url).content.decode()
                self.assertTrue(any(m in body for m in MARKERS), f"{url}: fixture not visible to B")

    def test_detail_routes_miss_the_other_cafe(self):
        ids = {
            "products": self.prod_b.pk, "variants": self.var_b.pk,
            "categories": self.cat_b.pk, "manufacturers": self.man_b.pk,
            "customers": self.cust_b.pk, "debts": self.debt_b.pk, "sales": self.sale_b.pk,
            "staff": self.emp_b.pk, "purchase-orders": self.po_b.pk, "orders": self.order_b.pk,
            "inventory-items": self.inv_b.pk, "shifts": self.shift_b.pk,
            "inventory-categories": self.invcat_b.pk, "suppliers": self.sup_b.pk,
            "expense-categories": self.expcat_b.pk, "expenses": self.exp_b.pk,
            "recurring-expenses": self.rexp_b.pk,
        }
        for base, pk in ids.items():
            url = f"/api/v1/{base}/{pk}/"
            with self.subTest(url=url):
                self.assertEqual(self.A.get(url).status_code, 404)
                self.assertIn(self.A.patch(url, {"name": "hijack"}, format="json").status_code,
                              (403, 404, 405))
                self.assertIn(self.A.delete(url).status_code, (403, 404, 405))
        for url in (
            f"/api/v1/customers/{self.cust_b.pk}/points/",
            f"/api/v1/customers/{self.cust_b.pk}/profile/",
            f"/api/v1/sales/{self.sale_b.pk}/revisions/",
            f"/api/v1/inventory-items/{self.inv_b.pk}/ledger/",
            f"/api/v1/inventory-items/{self.inv_b.pk}/moves/",
            f"/api/v1/products/{self.prod_b.pk}/recipe/",
            f"/api/v1/reports/items/{self.prod_b.pk}/",
            f"/api/v1/cash-drawer/{self.drawer_b.pk}/",
        ):
            with self.subTest(url=url):
                r = self.A.get(url)
                # the recipe view answers 400 "غير موجود" — still a miss
                self.assertIn(r.status_code, (400, 403, 404), r.content[:200])
                self.assertBlind(r, url)


class WriteIsolation(TwoCafes):
    def snapshot(self):
        return {
            "product": models.Product.objects.unscoped().filter(pk=self.prod_b.pk)
            .values("name", "price", "is_active").first(),
            "customer": models.Customer.objects.unscoped().filter(pk=self.cust_b.pk)
            .values("name", "phone").first(),
            "sale": models.Sale.objects.unscoped().filter(pk=self.sale_b.pk)
            .values("discounted_total").first(),
            "order": models.Order.objects.unscoped().filter(pk=self.order_b.pk)
            .values("status").first(),
            "inv": models.InventoryItem.objects.unscoped().filter(pk=self.inv_b.pk)
            .values("name").first(),
            "moves": models.StockMove.objects.unscoped().filter(store=self.b).count(),
            "returns": models.SaleReturn.objects.unscoped().filter(store=self.b).count(),
            "staff_pw": User.objects.get(pk=self.emp_b.pk).password,
            "po": models.PurchaseOrder.objects.unscoped().filter(pk=self.po_b.pk)
            .values("status").first(),
        }

    def test_actions_aimed_at_the_other_cafe_change_nothing(self):
        before = self.snapshot()
        for url, body in (
            (f"/api/v1/customers/{self.cust_b.pk}/settle/", {"amount": "1"}),
            (f"/api/v1/sales/{self.sale_b.pk}/returns/", {"reason": "x", "items": []}),
            (f"/api/v1/sales/{self.sale_b.pk}/revisions/1/restore/", {}),
            (f"/api/v1/staff/{self.emp_b.pk}/reset-password/", {"password": "hijack123"}),
            (f"/api/v1/purchase-orders/{self.po_b.pk}/receive/", {}),
            (f"/api/v1/orders/{self.order_b.pk}/advance/", {}),
            (f"/api/v1/inventory-items/{self.inv_b.pk}/purchase/", {"quantity": "5", "cost": "1"}),
            (f"/api/v1/inventory-items/{self.inv_b.pk}/waste/", {"quantity": "1"}),
            ("/api/v1/products/bulk_delete/", {"ids": [self.prod_b.pk]}),
            ("/api/v1/products/bulk_update/", {"ids": [self.prod_b.pk], "price": "1"}),
            ("/api/v1/sales/bulk_delete/", {"ids": [self.sale_b.pk]}),
            (f"/api/v1/products/{self.prod_b.pk}/recipe/", {"lines": []}),
        ):
            with self.subTest(url=url):
                r = self.A.post(url, body, format="json")
                if r.status_code == 405:
                    r = self.A.put(url, body, format="json")
                self.assertLess(r.status_code, 500, r.content[:300])
                self.assertBlind(r, url)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(models.RecipeLine.objects.unscoped().filter(pk=self.recipe_b.pk).exists())

    def test_references_to_the_other_cafes_rows_are_refused(self):
        attempts = [
            ("/api/v1/sales/", {"payment_method": "cash",
                                "items": [{"product": self.prod_b.pk, "quantity": 1}]}),
            ("/api/v1/sales/", {"payment_method": "cash",
                                "items": [{"product": self.latte_a.pk, "variant": self.var_b.pk,
                                           "quantity": 1}]}),
            ("/api/v1/sales/", {"payment_method": "cash", "customer": self.cust_b.pk,
                                "items": [{"product": self.latte_a.pk, "quantity": 1}]}),
            ("/api/v1/purchase-orders/", {"supplier_ref": self.sup_b.pk,
                                          "items": [{"product": self.prod_b.pk, "quantity": 1,
                                                     "unit_cost": "1"}]}),
            ("/api/v1/products/", {"name": "x", "price": "1", "category": self.cat_b.pk}),
            ("/api/v1/inventory-items/", {"name": "x", "category": self.invcat_b.pk,
                                          "supplier": self.sup_b.pk}),
            ("/api/v1/expenses/", {"category": self.expcat_b.pk, "amount": "1",
                                   "period": str(date.today().replace(day=1))}),
            ("/api/v1/recurring-expenses/", {"category": self.expcat_b.pk, "amount": "1",
                                             "start_month": str(date.today().replace(day=1))}),
            ("/api/v1/variants/", {"product": self.prod_b.pk, "label": "x", "price": "1"}),
            ("/api/v1/debts/", {"customer": self.cust_b.pk, "amount": "1"}),
        ]
        b_counts = self._b_counts()
        for url, body in attempts:
            with self.subTest(url=url, body=body):
                r = self.A.post(url, body, format="json")
                self.assertLess(r.status_code, 500, r.content[:300])
                self.assertBlind(r, url)
                if r.status_code in (200, 201):
                    # Accepted: then nothing of café B may be linked to the new row.
                    self._assert_no_link_to_b(url, r.json())
        self.assertEqual(self._b_counts(), b_counts, "a café-A write landed in café B")
        r = self.A.put(
            f"/api/v1/products/{self.latte_a.pk}/recipe/",
            {"lines": [{"item": self.inv_b.pk, "quantity": "1"}]}, format="json",
        )
        self.assertLess(r.status_code, 500)
        self.assertFalse(
            models.RecipeLine.objects.unscoped().filter(product=self.latte_a, item=self.inv_b).exists()
        )

    def _b_counts(self):
        return {
            m.__name__: m.objects.unscoped().filter(store=self.b).count()
            for m in (models.Sale, models.PurchaseOrder, models.Product, models.InventoryItem,
                      models.Expense, models.RecurringExpense, models.Debt, models.Customer)
        }

    def _assert_no_link_to_b(self, url, data):
        rid = data.get("id")
        if not rid:
            return
        b_ids = {
            "category": self.cat_b.pk, "supplier": self.sup_b.pk, "customer": self.cust_b.pk,
        }
        model = {
            "/api/v1/products/": models.Product, "/api/v1/inventory-items/": models.InventoryItem,
            "/api/v1/expenses/": models.Expense, "/api/v1/recurring-expenses/": models.RecurringExpense,
            "/api/v1/purchase-orders/": models.PurchaseOrder, "/api/v1/debts/": models.Debt,
            "/api/v1/sales/": models.Sale,
        }.get(url)
        if model is None:
            return
        row = model.objects.unscoped().get(pk=rid)
        self.assertEqual(row.store_id, self.a.pk)
        for field, b_pk in b_ids.items():
            if hasattr(row, f"{field}_id"):
                self.assertNotEqual(getattr(row, f"{field}_id"), b_pk, f"{url} linked café B's {field}")
        if model is models.Expense or model is models.RecurringExpense:
            self.assertNotEqual(row.category_id, self.expcat_b.pk)


class LoginIsolation(TwoCafes):
    def login(self, password, store):
        return APIClient().post(
            "/api/v1/auth/login/", {"username": "owner", "password": password, "store": store},
            format="json",
        )

    def test_same_username_logs_into_its_own_cafe_only(self):
        r = self.login("pa", "cafe-a")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["user"]["store_slug"], "cafe-a")
        r = self.login("pb", "cafe-b")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["user"]["store_slug"], "cafe-b")
        # B's password on A's door, and the reverse, must fail.
        self.assertNotEqual(self.login("pb", "cafe-a").status_code, 200)
        self.assertNotEqual(self.login("pa", "cafe-b").status_code, 200)

    def test_public_menu_shows_only_the_asked_cafe(self):
        body = APIClient().get("/api/v1/public/menu/?store=cafe-a").content.decode()
        self.assertIn("لاتيه", body)
        for m in MARKERS:
            self.assertNotIn(m, body)
        body_b = APIClient().get("/api/v1/public/menu/?store=cafe-b").content.decode()
        self.assertIn("ZZB-latte", body_b)
        self.assertNotIn("لاتيه", body_b)


class AppCustomersAreSingleCafe(TwoCafes):
    """Documents TODAY's limit, so nobody assumes otherwise: the customer app
    endpoints (/shop/*, Firebase/Clerk sync) serve the ONE café named by
    CLERK_STORE_SLUG. A second café can use the POS, menu, reports and staff
    fully isolated, but not the loyalty phone app, until those endpoints take
    the store from the request."""

    def test_shop_endpoints_are_pinned_to_the_deployments_cafe(self):
        import inspect

        from apps.store import views

        for name in ("ShopMeView", "ShopOrdersView", "ShopDeviceView", "ShopUsualView",
                     "ShopNotificationsView", "ShopOrderCancelView"):
            with self.subTest(view=name):
                self.assertIn("CLERK_STORE_SLUG", inspect.getsource(getattr(views, name)))

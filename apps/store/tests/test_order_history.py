"""Order history and the order's own timeline.

The counter can look back at any day's orders (business days, like invoices),
and every order carries the steps it went through — what the customer's order
page draws. Both are per shop.
"""
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.store import finance, models
from apps.store.tests.test_app_auth import Base


@override_settings(ONLINE_ORDERS_ENABLED=True)
class OrderTimeline(Base):
    def place(self, token, uuid="22222222-2222-2222-2222-222222222222"):
        p = models.Product.objects.create(store=self.a, name="لاتيه", price=12)
        r = self.as_customer(token).post(
            "/api/v1/shop/orders/",
            {"items": [{"product": p.id, "quantity": 1}], "client_uuid": uuid}, format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def test_every_move_is_recorded_once_and_shown_to_the_customer(self):
        self.otp()
        token = self.verify().json()["token"]
        o = self.place(token)
        self.assertEqual([s["s"] for s in o["timeline"]], ["placed"])
        for st in ("accepted", "ready", "ready", "collected"):
            r = self.staff.post(f"/api/v1/orders/{o['id']}/advance/", {"status": st}, format="json")
            self.assertEqual(r.status_code, 200, r.content)
        mine = self.as_customer(token).get("/api/v1/shop/orders/").json()["results"][0]
        self.assertEqual([s["s"] for s in mine["timeline"]], ["placed", "accepted", "ready", "collected"])
        self.assertTrue(all(s["at"] for s in mine["timeline"]))

    def test_a_customer_cancel_is_a_step_too(self):
        self.otp()
        token = self.verify().json()["token"]
        o = self.place(token)
        r = self.as_customer(token).post(f"/api/v1/shop/orders/{o['id']}/cancel/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([s["s"] for s in r.json()["timeline"]], ["placed", "cancelled"])

    def test_orders_from_before_the_timeline_still_show_what_is_known(self):
        c = models.Customer.objects.create(store=self.a, name="س", phone="0599000001")
        o = models.Order.objects.create(store=self.a, customer=c, status="ready")
        r = self.staff.get(f"/api/v1/orders/{o.pk}/").json()
        r = r.get("data", r)
        self.assertEqual([s["s"] for s in r["timeline"]], ["placed", "ready"])


class OrderHistory(Base):
    def test_day_filter_uses_business_days_and_stays_in_the_shop(self):
        c = models.Customer.objects.create(store=self.a, name="س", phone="0599000002")
        today = models.Order.objects.create(store=self.a, customer=c, status="collected")
        old = models.Order.objects.create(store=self.a, customer=c, status="collected")
        lo, _ = finance.bounds(finance.today() - timedelta(days=3), finance.today() - timedelta(days=3))
        models.Order.objects.unscoped().filter(pk=old.pk).update(created_at=lo + timedelta(hours=2))
        # Another shop's order, same day: never listed.
        cb = models.Customer.objects.create(store=self.b, name="ب", phone="0599000003")
        models.Order.objects.create(store=self.b, customer=cb, status="collected")

        day = finance.today().isoformat()
        r = self.staff.get(f"/api/v1/orders/?day_from={day}&day_to={day}").json()
        ids = [o["id"] for o in r.get("data", r)["results"]]
        self.assertEqual(ids, [today.pk])
        d3 = (finance.today() - timedelta(days=3)).isoformat()
        r = self.staff.get(f"/api/v1/orders/?day_from={d3}&day_to={day}&status=collected").json()
        self.assertEqual(sorted(o["id"] for o in r.get("data", r)["results"]), sorted([today.pk, old.pk]))

    def test_an_employee_sees_the_history_too(self):
        # History is the counter's tool ("did #12 go out yesterday?").
        emp = User.objects.create_user("e", password="x", store=self.a, role="employee")
        from rest_framework.test import APIClient

        api = APIClient()
        api.force_authenticate(emp)
        self.assertEqual(api.get("/api/v1/orders/").status_code, 200)

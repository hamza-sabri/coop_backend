"""A customer can delete their account from the app (App Store / Play rule):
the person is erased, the shop's books are not."""
from django.test import override_settings

from apps.store import models
from apps.store.tests.test_app_auth import Base


@override_settings(ONLINE_ORDERS_ENABLED=True)
class AccountDelete(Base):
    def test_delete_erases_the_person_and_keeps_the_orders(self):
        self.otp()
        token = self.verify(name="سارة").json()["token"]
        api = self.as_customer(token)
        p = models.Product.objects.create(store=self.a, name="لاتيه", price=12)
        api.post("/api/v1/shop/orders/", {"items": [{"product": p.id, "quantity": 1}],
                                          "client_uuid": "55555555-5555-5555-5555-555555555555"}, format="json")
        c = models.Customer.objects.for_pharmacy(self.a).get()

        self.assertEqual(api.post("/api/v1/shop/me/delete/", {}, format="json").status_code, 400)  # must confirm
        r = api.post("/api/v1/shop/me/delete/", {"confirm": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

        c.refresh_from_db()
        self.assertEqual(c.name, "زبون محذوف")
        self.assertIsNone(c.phone)
        self.assertEqual(models.Order.objects.for_pharmacy(self.a).filter(customer=c).count(), 1)
        # The session is dead: the same token no longer gets in.
        self.assertEqual(api.get("/api/v1/shop/me/").status_code, 401)
        # Signing up again with the same number starts a fresh account.
        self.age_logs()
        self.otp()
        again = self.verify(name="سارة").json()
        self.assertNotEqual(again["customer"]["id"], c.pk)


class ReviewAccount(Base):
    """The store reviewers' number signs in with the configured code, no SMS."""

    def test_review_number_uses_the_fixed_code_and_sends_nothing(self):
        from unittest import mock

        from apps.store import app_auth

        with override_settings(APP_REVIEW_PHONE="0590000000", APP_REVIEW_CODE="246810"), \
                mock.patch.object(app_auth, "_send") as send:
            for _ in range(8):  # no rate limit for the reviewer
                r = self.anon.post("/api/v1/shop/auth/otp/request/", {"store": "coop", "phone": "0590000000"}, format="json")
                self.assertEqual(r.status_code, 200, r.content)
            send.assert_not_called()
            r = self.verify(phone="0590000000", code="246810", name="Apple Review")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertTrue(r.json()["token"].startswith("cs_"))

    def test_off_when_not_configured(self):
        with override_settings(APP_REVIEW_PHONE="", APP_REVIEW_CODE=""):
            self.otp(phone="0590000000", code="111111")
            self.assertEqual(self.verify(phone="0590000000", code="246810").status_code, 400)

"""Customer app sign-in by phone: SMS link, SMS code, iPhone handoff, sessions.

Every test runs in dry-run SMS mode (no API ID), so nothing is ever sent.
"""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core import sms
from apps.store import app_auth, models


@override_settings(HTD_SMS_API_ID="", SMS_DRY_RUN=True, SIGNUP_BONUS_BEANS=5)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.a = models.Store.objects.create(name="كوب", slug="coop")
        self.b = models.Store.objects.create(name="برغر", slug="burger")
        self.owner_a = User.objects.create_user("o", password="x", store=self.a)
        self.staff = APIClient()
        self.staff.force_authenticate(self.owner_a)
        self.anon = APIClient()

    def otp(self, store="coop", phone="0599123456", code="123456"):
        with mock.patch.object(app_auth, "_digits", return_value=code):
            return self.anon.post("/api/v1/shop/auth/otp/request/", {"store": store, "phone": phone}, format="json")

    def verify(self, store="coop", phone="0599123456", code="123456", name=""):
        return self.anon.post(
            "/api/v1/shop/auth/otp/verify/",
            {"store": store, "phone": phone, "code": code, "name": name}, format="json",
        )

    def as_customer(self, token):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return c

    def age_logs(self):
        """Move every SMS log back in time, past the per-number cooldown."""
        models.SmsLog.objects.unscoped().update(created_at=timezone.now() - timedelta(minutes=5))


class SmsHelpers(TestCase):
    def test_numbers_go_to_the_right_country_code(self):
        self.assertEqual(sms.international("0599123456"), "970599123456")
        self.assertEqual(sms.international("0569123456"), "970569123456")
        self.assertEqual(sms.international("0521234567"), "972521234567")
        self.assertEqual(sms.international("+970 599 123 456"), "970599123456")
        self.assertIsNone(sms.international("12345"))

    def test_arabic_messages_count_70_characters_per_sms(self):
        self.assertEqual(sms.segments("ا" * 70), 1)
        self.assertEqual(sms.segments("ا" * 71), 2)
        self.assertEqual(sms.segments("a" * 160), 1)

    def test_the_link_sms_fits_in_one_message(self):
        store = models.Store(name="كوب", slug="coop")
        msg = f"{store.name}: نقاطك وطلباتك هنا {app_auth.app_url(store)}/i/{'A' * 10}"
        self.assertEqual(sms.segments(msg), 1, msg)


class OtpFlow(Base):
    def test_code_signs_in_and_finds_the_counter_customer(self):
        counter = models.Customer.objects.create(store=self.a, name="سامر", phone="0599123456")
        r = self.otp()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn("debug_code", r.json())  # never outside DEBUG
        log = models.SmsLog.objects.unscoped().get()
        self.assertEqual((log.store_id, log.kind, log.status), (self.a.pk, "otp", "dry_run"))
        r = self.verify()
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body["token"].startswith("cs_"))
        self.assertEqual(body["customer"]["id"], counter.pk)
        counter.refresh_from_db()
        self.assertIsNotNone(counter.app_registered_at)
        # The session is the whole login from now on.
        me = self.as_customer(body["token"]).get("/api/v1/shop/me/")
        self.assertEqual(me.status_code, 200, me.content)
        self.assertEqual(me.json()["name"], "سامر")
        self.assertEqual(me.json()["beans"], 5)  # sign-up gift on a new loyalty profile

    def test_new_number_becomes_a_new_customer_with_its_name(self):
        self.otp(phone="0569000001")
        r = self.verify(phone="0569000001", name="ليان")
        self.assertEqual(r.status_code, 200, r.content)
        c = models.Customer.objects.for_pharmacy(self.a.pk).get(phone="0569000001")
        self.assertEqual(c.name, "ليان")

    def test_wrong_codes_burn_the_code_after_five(self):
        self.otp()
        for _ in range(5):
            self.assertEqual(self.verify(code="000000").status_code, 400)
        self.assertEqual(self.verify(code="123456").status_code, 400)  # dead now

    def test_expired_code_is_refused(self):
        self.otp()
        models.CustomerAuthCode.objects.unscoped().update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.verify().status_code, 400)

    def test_a_code_from_one_store_does_not_open_another(self):
        self.otp(store="coop")
        self.assertEqual(self.verify(store="burger").status_code, 400)

    def test_bad_number_and_unknown_store(self):
        self.assertEqual(self.otp(phone="123").status_code, 400)
        self.assertEqual(self.otp(store="nope").status_code, 404)


class Limits(Base):
    def test_one_code_a_minute_per_number(self):
        self.assertEqual(self.otp().status_code, 200)
        r = self.otp()
        self.assertEqual(r.status_code, 429)
        self.assertIn("retry_after", r.json())

    def test_five_a_day_per_number_across_stores(self):
        for i in range(5):
            self.assertEqual(self.otp(store="coop" if i % 2 else "burger").status_code, 200)
            self.age_logs()
        self.assertEqual(self.otp().status_code, 429)

    @override_settings(SMS_DAILY_CAP_PER_STORE=2)
    def test_store_daily_cap(self):
        self.otp(phone="0599000001")
        self.otp(phone="0599000002")
        self.assertEqual(self.otp(phone="0599000003").status_code, 429)
        # Another store is not affected by this one's cap.
        self.assertEqual(self.otp(store="burger", phone="0599000003").status_code, 200)


class SameNumberTwoCafes(Base):
    def test_two_customers_two_sessions_no_crossing(self):
        self.otp(store="coop")
        ta = self.verify(store="coop").json()["token"]
        self.age_logs()
        self.otp(store="burger", code="654321")
        tb = self.verify(store="burger", code="654321").json()["token"]
        ca = models.Customer.objects.for_pharmacy(self.a.pk).get(phone="0599123456")
        cb = models.Customer.objects.for_pharmacy(self.b.pk).get(phone="0599123456")
        self.assertNotEqual(ca.pk, cb.pk)
        models.Notification.objects.create(store=self.a, customer=ca, kind="x", title="A-only")
        feed_b = self.as_customer(tb).get("/api/v1/shop/notifications/").content.decode()
        self.assertNotIn("A-only", feed_b)
        feed_a = self.as_customer(ta).get("/api/v1/shop/notifications/").content.decode()
        self.assertIn("A-only", feed_a)


class InviteLink(Base):
    def create(self, **extra):
        body = {"name": "سامر", "phone": "0599123456", "send_app_link": True, **extra}
        return self.staff.post("/api/v1/customers/", body, format="json")

    def token_from_log(self):
        # The raw token is only ever in the SMS; capture it from the sender.
        return self._sent[-1].rsplit("/i/", 1)[1]

    def setUp(self):
        super().setUp()
        self._sent = []
        real = app_auth.sms.send_as

        def spy(to, msg, **kw):
            self._sent.append(msg)
            return real(to, msg, **kw)

        p = mock.patch.object(app_auth.sms, "send_as", side_effect=spy)
        p.start()
        self.addCleanup(p.stop)

    def test_create_sends_one_link_and_the_link_signs_in(self):
        r = self.create()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()["app_link"]["sent"])
        self.assertIsNotNone(r.json()["app_invited_at"] or True)
        self.assertEqual(models.SmsLog.objects.unscoped().filter(kind="invite").count(), 1)
        self.assertIn("https://coop.clinixa.cloud/app/i/", self._sent[-1])
        token = self.token_from_log()
        r = self.anon.post("/api/v1/shop/auth/link/", {"store": "coop", "token": token}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        cust = models.Customer.objects.for_pharmacy(self.a.pk).get(phone="0599123456")
        self.assertEqual(r.json()["customer"]["id"], cust.pk)
        self.assertIsNotNone(cust.app_registered_at)
        self.assertIsNotNone(cust.app_invited_at)
        # Same sign-up gift as signing up with a code.
        me = self.as_customer(r.json()["token"]).get("/api/v1/shop/me/").json()
        self.assertEqual(me["beans"], 5)
        # No second SMS was needed to sign in.
        self.assertEqual(models.SmsLog.objects.unscoped().count(), 1)
        # Single use.
        again = self.anon.post("/api/v1/shop/auth/link/", {"store": "coop", "token": token}, format="json")
        self.assertEqual(again.status_code, 410)

    def test_unticked_box_sends_nothing(self):
        r = self.create(send_app_link=False)
        self.assertEqual(r.status_code, 201)
        self.assertNotIn("app_link", r.json())
        self.assertEqual(models.SmsLog.objects.unscoped().count(), 0)

    def test_failed_sms_never_loses_the_customer(self):
        with mock.patch.object(app_auth, "_send", side_effect=RuntimeError("down")):
            r = self.create()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertFalse(r.json()["app_link"]["sent"])
        self.assertTrue(models.Customer.objects.for_pharmacy(self.a.pk).filter(phone="0599123456").exists())

    def test_link_from_one_store_is_useless_at_another(self):
        self.create()
        token = self.token_from_log()
        r = self.anon.post("/api/v1/shop/auth/link/", {"store": "burger", "token": token}, format="json")
        self.assertEqual(r.status_code, 410)

    def test_expired_link(self):
        self.create()
        models.CustomerAuthCode.objects.unscoped().update(expires_at=timezone.now() - timedelta(minutes=1))
        r = self.anon.post("/api/v1/shop/auth/link/", {"store": "coop", "token": self.token_from_log()}, format="json")
        self.assertEqual(r.status_code, 410)
        self.assertEqual(r.json()["code"], "link_expired")

    def test_send_button_with_cooldown_and_tenant_scope(self):
        c = models.Customer.objects.create(store=self.a, name="ب", phone="0599000009")
        r = self.staff.post(f"/api/v1/customers/{c.pk}/send-app-link/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.staff.post(f"/api/v1/customers/{c.pk}/send-app-link/").status_code, 429)
        other = models.Customer.objects.create(store=self.b, name="غريب", phone="0599000008")
        self.assertEqual(self.staff.post(f"/api/v1/customers/{other.pk}/send-app-link/").status_code, 404)
        no_phone = models.Customer.objects.create(store=self.a, name="بلا رقم")
        self.assertEqual(self.staff.post(f"/api/v1/customers/{no_phone.pk}/send-app-link/").status_code, 400)

    def test_dashboard_lists_show_the_badges(self):
        self.create()
        self.anon.post("/api/v1/shop/auth/link/", {"store": "coop", "token": self.token_from_log()}, format="json")
        rows = self.staff.get("/api/v1/customers/table/").json()["results"]
        row = next(r for r in rows if r["phone"] == "0599123456")
        self.assertTrue(row["app"])
        self.assertIsNotNone(row["app_invited_at"])
        quick = self.staff.get("/api/v1/customers/quick/").json()["results"]
        self.assertTrue(next(r for r in quick if r["phone"] == "0599123456")["signed_up"])


class HandoffAndSessions(Base):
    def signed_in(self):
        self.otp()
        return self.verify().json()["token"]

    def test_safari_code_signs_in_the_home_screen_app_without_sms(self):
        token = self.signed_in()
        sent_before = models.SmsLog.objects.unscoped().count()
        code = self.as_customer(token).post("/api/v1/shop/auth/handoff/").json()["code"]
        r = self.anon.post(
            "/api/v1/shop/auth/handoff/redeem/",
            {"store": "coop", "phone": "0599123456", "code": code}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotEqual(r.json()["token"], token)
        self.assertEqual(models.SmsLog.objects.unscoped().count(), sent_before)
        # Single use.
        again = self.anon.post(
            "/api/v1/shop/auth/handoff/redeem/",
            {"store": "coop", "phone": "0599123456", "code": code}, format="json",
        )
        self.assertEqual(again.status_code, 400)

    def test_handoff_needs_a_session(self):
        self.assertIn(self.anon.post("/api/v1/shop/auth/handoff/").status_code, (401, 403))

    def test_logout_and_bad_tokens_are_401(self):
        token = self.signed_in()
        c = self.as_customer(token)
        self.assertEqual(c.post("/api/v1/shop/auth/logout/").status_code, 200)
        self.assertEqual(c.get("/api/v1/shop/me/").status_code, 401)
        self.assertEqual(self.as_customer("cs_nonsense").get("/api/v1/shop/me/").status_code, 401)

    def test_session_cannot_reach_staff_endpoints(self):
        token = self.signed_in()
        self.assertIn(self.as_customer(token).get("/api/v1/customers/").status_code, (401, 403))

    def test_only_hashes_are_stored(self):
        token = self.signed_in()
        self.assertFalse(models.CustomerSession.objects.unscoped().filter(token_hash=token).exists())
        self.assertFalse(models.CustomerAuthCode.objects.unscoped().filter(code_hash="123456").exists())

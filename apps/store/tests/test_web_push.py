"""Customer Web Push: subscribe per shop, one push per order move, dead
subscriptions pruned, and the message is real RFC 8291 (decrypted here)."""
import json
import os
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import override_settings

from apps.store import models, webpush
from apps.store.tests.test_app_auth import Base

PUB, PRIV = webpush.make_keys()


def browser():
    """A fake browser: its key pair and auth secret, like PushManager makes."""
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, webpush.b64u(pub), os.urandom(16)


def decrypt(body: bytes, key, auth: bytes) -> dict:
    """RFC 8291 receiver side (independent of webpush.encrypt)."""
    import hashlib
    import hmac

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, idlen = body[:16], body[20]
    as_public, ct = body[21:21 + idlen], body[21 + idlen:]
    ua_public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))

    def hk(s, ikm, info, n):
        prk = hmac.new(s, ikm, hashlib.sha256).digest()
        return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:n]

    ikm = hk(auth, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    plain = AESGCM(hk(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)).decrypt(
        hk(salt, ikm, b"Content-Encoding: nonce\x00", 12), ct, None)
    return json.loads(plain.rstrip(b"\x00")[:-1])


@override_settings(WEBPUSH_PUBLIC_KEY=PUB, WEBPUSH_PRIVATE_KEY=PRIV, ONLINE_ORDERS_ENABLED=True)
class WebPush(Base):
    def signed_in(self, store="coop", phone="0599123456"):
        self.otp(store=store, phone=phone)
        return self.as_customer(self.verify(store=store, phone=phone).json()["token"])

    def subscribe(self, api, endpoint="https://push.example.com/abc"):
        key, p256dh, auth = browser()
        r = api.post("/api/v1/shop/push/", {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": webpush.b64u(auth)}},
                     format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return key, auth

    def test_key_is_public_and_sound_is_the_shops(self):
        api = self.signed_in()
        body = api.get("/api/v1/shop/push/").json()
        self.assertEqual(body["key"], PUB)
        self.assertEqual(body["sound"], "coop")
        self.a.app_settings = {"sound": "bell"}
        self.a.save()
        self.assertEqual(api.get("/api/v1/shop/push/").json()["sound"], "bell")

    def test_each_order_move_is_pushed_encrypted_to_the_customer(self):
        api = self.signed_in()
        key, auth = self.subscribe(api)
        p = models.Product.objects.create(store=self.a, name="لاتيه", price=12)
        sent = []

        def fake_post(url, data=None, headers=None, timeout=None):
            sent.append((url, data, headers))
            return mock.Mock(status_code=201, text="")

        with mock.patch("requests.post", side_effect=fake_post), self.captureOnCommitCallbacks(execute=True):
            o = api.post("/api/v1/shop/orders/", {"items": [{"product": p.id, "quantity": 1}],
                                                  "client_uuid": "33333333-3333-3333-3333-333333333333"},
                         format="json").json()
            self.staff.post(f"/api/v1/orders/{o['id']}/advance/", {"status": "ready"}, format="json")
        self.assertEqual(len(sent), 2)
        url, body, headers = sent[-1]
        self.assertEqual(url, "https://push.example.com/abc")
        self.assertEqual(headers["Content-Encoding"], "aes128gcm")
        self.assertTrue(headers["Authorization"].startswith("vapid t="))
        msg = decrypt(body, key, auth)
        self.assertEqual(msg["title"], "طلبك جاهز")
        self.assertEqual(msg["tag"], f"order-{o['id']}")
        self.assertEqual(msg["sound"], "coop")
        self.assertIn(f"#order={o['id']}", msg["url"])
        self.assertIsNotNone(models.Notification.objects.unscoped().filter(pushed_at__isnull=False).first())

    def test_gone_subscriptions_are_removed(self):
        api = self.signed_in()
        self.subscribe(api)
        c = models.Customer.objects.for_pharmacy(self.a).get()
        from apps.store import push

        with mock.patch("requests.post", return_value=mock.Mock(status_code=410, text="")), \
                self.captureOnCommitCallbacks(execute=True):
            push.notify(self.a, c, "order_ready", "x")
        self.assertFalse(models.PushSubscription.objects.unscoped().exists())

    def test_one_browser_follows_two_shops(self):
        a = self.signed_in("coop")
        b = self.signed_in("burger", phone="0599123457")
        self.subscribe(a, "https://push.example.com/same")
        self.subscribe(b, "https://push.example.com/same")
        self.assertEqual(models.PushSubscription.objects.unscoped().count(), 2)
        # Another shop's customer is never pushed this shop's news.
        ca = models.Customer.objects.for_pharmacy(self.a).get()
        from apps.store import push

        with mock.patch("requests.post", return_value=mock.Mock(status_code=201, text="")) as post, \
                self.captureOnCommitCallbacks(execute=True):
            push.notify(self.a, ca, "order_ready", "x")
        self.assertEqual(post.call_count, 1)

    def test_unsubscribe(self):
        api = self.signed_in()
        self.subscribe(api)
        api.delete("/api/v1/shop/push/", {"endpoint": "https://push.example.com/abc"}, format="json")
        self.assertFalse(models.PushSubscription.objects.unscoped().exists())

    def test_bad_subscription_is_refused(self):
        api = self.signed_in()
        r = api.post("/api/v1/shop/push/", {"endpoint": "http://insecure", "keys": {}}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_owner_sets_the_sound_employees_cannot(self):
        r = self.staff.put("/api/v1/store/app-settings/", {"sound": "bell"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.a.refresh_from_db()
        self.assertEqual(self.a.app_settings["sound"], "bell")
        self.assertEqual(self.staff.put("/api/v1/store/app-settings/", {"sound": "x"}, format="json").status_code, 400)
        from rest_framework.test import APIClient

        from apps.accounts.models import User

        emp = APIClient()
        emp.force_authenticate(User.objects.create_user("emp", password="x", store=self.a, role="employee"))
        self.assertEqual(emp.put("/api/v1/store/app-settings/", {"sound": "chime"}, format="json").status_code, 403)


@override_settings(WEBPUSH_PUBLIC_KEY="", WEBPUSH_PRIVATE_KEY="")
class KeyPasting(Base):
    """Keys pasted into the host's env with quotes, spaces or a wrong public
    half still give browsers the RIGHT key (derived from the private one)."""

    def test_quotes_spaces_and_a_wrong_public_key_are_survived(self):
        other_pub, _ = webpush.make_keys()
        with override_settings(WEBPUSH_PUBLIC_KEY=f'"{other_pub}"', WEBPUSH_PRIVATE_KEY=f' "{PRIV}"\n'):
            self.assertTrue(webpush.configured())
            self.assertEqual(webpush.public_key(), PUB)
        with override_settings(WEBPUSH_PUBLIC_KEY=f"'{PUB}'", WEBPUSH_PRIVATE_KEY="not-a-key"):
            self.assertFalse(webpush.configured())


@override_settings(WEBPUSH_PUBLIC_KEY="", WEBPUSH_PRIVATE_KEY="")
class WithoutKeys(Base):
    def test_nothing_is_sent_and_nothing_breaks(self):
        c = models.Customer.objects.create(store=self.a, name="س", phone="0599000009")
        models.PushSubscription.objects.create(store=self.a, customer=c, endpoint="https://p/x", p256dh="a", auth="b")
        from apps.store import push

        with mock.patch("requests.post") as post, self.captureOnCommitCallbacks(execute=True):
            self.assertIsNotNone(push.notify(self.a, c, "order_ready", "x"))
        post.assert_not_called()


@override_settings(WEBPUSH_PUBLIC_KEY=PUB, WEBPUSH_PRIVATE_KEY=PRIV, ONLINE_ORDERS_ENABLED=True)
class StaffPush(Base):
    """A new order buzzes the EMPLOYEES' phones (admin closed, phone locked);
    owners and superusers are never alerted."""

    def staff_api(self, user):
        from rest_framework.test import APIClient

        api = APIClient()
        api.force_authenticate(user)
        return api

    def sub(self, api, endpoint):
        key, p256dh, auth = browser()
        return api.post("/api/v1/store/staff-push/", {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": webpush.b64u(auth)}},
                        format="json"), key, auth

    def test_only_employees_subscribe_and_get_new_orders(self):
        from apps.accounts.models import User

        emp = User.objects.create_user("emp1", password="x", store=self.a, role="employee")
        other_shop_emp = User.objects.create_user("emp2", password="x", store=self.b, role="employee")
        body = self.staff.get("/api/v1/store/staff-push/").json()
        self.assertFalse(body.get("data", body)["eligible"])  # owner
        r, _, _ = self.sub(self.staff, "https://push.example.com/owner")
        self.assertEqual(r.status_code, 403)
        r, key, auth = self.sub(self.staff_api(emp), "https://push.example.com/emp")
        self.assertEqual(r.status_code, 200, r.content)
        r, _, _ = self.sub(self.staff_api(other_shop_emp), "https://push.example.com/other")
        self.assertEqual(r.status_code, 200)

        self.otp()
        cust = self.as_customer(self.verify().json()["token"])
        p = models.Product.objects.create(store=self.a, name="لاتيه", price=12)
        sent = []

        def fake_post(url, data=None, headers=None, timeout=None):
            sent.append((url, data))
            return mock.Mock(status_code=201, text="")

        with mock.patch("requests.post", side_effect=fake_post), self.captureOnCommitCallbacks(execute=True):
            o = cust.post("/api/v1/shop/orders/", {"items": [{"product": p.id, "quantity": 2}], "fulfilment": "dinein",
                                                    "table_number": "4", "client_uuid": "44444444-4444-4444-4444-444444444444"},
                          format="json").json()
        staff_sent = [d for u, d in sent if u == "https://push.example.com/emp"]
        self.assertEqual(len(staff_sent), 1)
        self.assertFalse(any(u == "https://push.example.com/other" for u, _ in sent))
        msg = decrypt(staff_sent[0], key, auth)
        self.assertEqual(msg["title"], f"🔔 طلب جديد #{o['id']}")
        self.assertIn("طاولة 4", msg["body"])
        self.assertIn("2× لاتيه", msg["body"])
        self.assertTrue(msg["requireInteraction"])
        self.assertEqual(msg["url"], "/live")


    def test_reminders_repeat_until_the_order_is_accepted(self):
        from apps.store import push

        p = models.Product.objects.create(store=self.a, name="لاتيه", price=12)
        self.otp()
        cust = self.as_customer(self.verify().json()["token"])
        o = cust.post("/api/v1/shop/orders/", {"items": [{"product": p.id, "quantity": 1}],
                                                "client_uuid": "55555555-5555-5555-5555-555555555555"},
                      format="json").json()
        calls, naps = [], []

        def deliver(reminder=0):
            calls.append(reminder)
            if reminder == 2:  # someone accepts it after the 2nd reminder
                self.staff.post(f"/api/v1/orders/{o['id']}/advance/", {"status": "accepted"}, format="json")

        with override_settings(STAFF_REMIND_SECONDS=30, STAFF_REMIND_TIMES=10):
            n = push.remind_until_accepted(o["id"], deliver, sleep=naps.append)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(n, 2)
        self.assertEqual(naps, [30, 30, 30])

    def test_reminders_stop_after_the_limit(self):
        from apps.store import push

        p = models.Product.objects.create(store=self.a, name="لاتيه", price=12)
        self.otp()
        cust = self.as_customer(self.verify().json()["token"])
        o = cust.post("/api/v1/shop/orders/", {"items": [{"product": p.id, "quantity": 1}],
                                                "client_uuid": "66666666-6666-6666-6666-666666666666"},
                      format="json").json()
        calls = []
        with override_settings(STAFF_REMIND_TIMES=3):
            push.remind_until_accepted(o["id"], lambda reminder=0: calls.append(reminder), sleep=lambda s: None)
        self.assertEqual(calls, [1, 2, 3])


class NativeAppPush(Base):
    """The Flutter app (FCM): the shop's sound rides on every message."""

    @override_settings(FIREBASE_PROJECT_ID="p")
    def test_fcm_message_uses_the_shops_sound_channel(self):
        from apps.store import push

        sent = []
        with mock.patch.object(push, "_access_token", return_value="tok"), \
                mock.patch("requests.post", side_effect=lambda url, headers=None, json=None, timeout=None:
                           sent.append(json) or mock.Mock(status_code=200, text="")):
            self.assertEqual(push._send_one("t", "طلبك جاهز", "", {"order_id": 3}, "bell"), "ok")
            push._send_one("t", "x", "", {}, "off")
        m = sent[0]["message"]
        self.assertEqual(m["android"]["notification"]["channel_id"], "koup_bell")
        self.assertEqual(m["apns"]["payload"]["aps"]["sound"], "bell.caf")
        self.assertEqual(m["data"]["sound"], "bell")
        off = sent[1]["message"]
        self.assertEqual(off["android"]["notification"]["channel_id"], "koup_off")
        self.assertNotIn("sound", off["apns"]["payload"]["aps"])

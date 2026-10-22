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
class WithoutKeys(Base):
    def test_nothing_is_sent_and_nothing_breaks(self):
        c = models.Customer.objects.create(store=self.a, name="س", phone="0599000009")
        models.PushSubscription.objects.create(store=self.a, customer=c, endpoint="https://p/x", p256dh="a", auth="b")
        from apps.store import push

        with mock.patch("requests.post") as post, self.captureOnCommitCallbacks(execute=True):
            self.assertIsNotNone(push.notify(self.a, c, "order_ready", "x"))
        post.assert_not_called()

"""The online-ordering switch.

Off by default, and off means off everywhere at once: the admin board goes
quiet AND the order endpoint refuses. Switching off only the board would let
customers keep placing orders that nobody is watching for — the failure this
file exists to prevent.
"""
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.store import models
from apps.store.modules import effective_modules, online_orders_open, pharmacy_modules


class OnlineOrdersSwitchTests(TestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="كوب", slug="koup")
        self.client = APIClient()

    @override_settings(ONLINE_ORDERS_ENABLED=False)
    def test_off_by_default_even_for_a_legacy_everything_store(self):
        # enabled_modules == [] means "everything" — the switch must still win.
        self.assertEqual(self.store.enabled_modules or [], [])
        self.assertNotIn("online_orders", pharmacy_modules(self.store))
        self.assertFalse(online_orders_open(self.store))

    @override_settings(ONLINE_ORDERS_ENABLED=True)
    def test_on_when_the_switch_is_on(self):
        self.assertTrue(online_orders_open(self.store))

    @override_settings(ONLINE_ORDERS_ENABLED=False, CLERK_STORE_SLUG="koup")
    def test_public_endpoint_says_closed(self):
        r = self.client.get("/api/v1/public/ordering/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"open": False})

    @override_settings(ONLINE_ORDERS_ENABLED=True, CLERK_STORE_SLUG="koup")
    def test_public_endpoint_says_open(self):
        self.assertEqual(self.client.get("/api/v1/public/ordering/").json(), {"open": True})

    @override_settings(ONLINE_ORDERS_ENABLED=False, CLERK_STORE_SLUG="nope")
    def test_unknown_store_is_closed_not_an_error(self):
        self.assertEqual(self.client.get("/api/v1/public/ordering/").json(), {"open": False})

    @override_settings(ONLINE_ORDERS_ENABLED=False)
    def test_staff_never_see_the_module(self):
        from apps.accounts.models import User

        u = User.objects.create_user(username="owner", password="x", store=self.store)
        self.assertNotIn("online_orders", effective_modules(u))

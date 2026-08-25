"""Billing enforcement: a suspended store (is_active=False) is locked out.

This is the technical half of the collection runbook — when a tenant doesn't
pay, flipping `is_active` off must cleanly deny access, both at login and for
any already-issued access token.

Run: python manage.py test apps.store.tests.test_billing_suspend
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()


class SuspendTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.active = models.Store.objects.create(name="Active", slug="live-ph", is_active=True)
        cls.suspended = models.Store.objects.create(
            name="Suspended", slug="dead-ph", is_active=False
        )
        cls.u_active = User.objects.create_user("pays", password="pw", store=cls.active)
        cls.u_suspended = User.objects.create_user("owes", password="pw", store=cls.suspended)
        models.Product.objects.create(
            store=cls.active, name="A", price=Decimal("1.00"), stock=1
        )
        models.Product.objects.create(
            store=cls.suspended, name="B", price=Decimal("1.00"), stock=1
        )

    def setUp(self):
        cache.clear()

    def test_active_tenant_can_use_the_api(self):
        c = APIClient()
        c.force_authenticate(self.u_active)
        self.assertEqual(c.get("/api/v1/products/").status_code, 200)

    def test_suspended_tenant_is_denied_even_with_a_valid_token(self):
        c = APIClient()
        c.force_authenticate(self.u_suspended)  # simulates an unexpired token
        for url in ("/api/v1/products/", "/api/v1/sales/", "/api/v1/customers/"):
            with self.subTest(url=url):
                self.assertEqual(c.get(url).status_code, 403)

    def test_suspended_tenant_cannot_log_in(self):
        c = APIClient()
        r = c.post(
            "/api/v1/auth/login/",
            {"username": "owes", "password": "pw"},
            format="json",
        )
        self.assertEqual(r.status_code, 401)

    def test_active_tenant_can_log_in(self):
        c = APIClient()
        r = c.post(
            "/api/v1/auth/login/",
            {"username": "pays", "password": "pw"},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("access", r.json())

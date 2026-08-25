"""Owner-managed staff API — /api/v1/staff/.

Guards asserted here (any failure = do not ship):
- owner-only (employees 403);
- tenant-scoped BY HAND (accounts.User has no TenantManager) — no list/get/
  update/reset can cross the store boundary;
- no privilege escalation beyond the store's own module tier;
- never lock the store out of its last owner / deactivate yourself;
- username unique within the store;
- no hard delete (soft is_active only).

Requests use format="json": allowed_modules is a JSONField, and the test
client's default multipart encoding can't carry a JSON list (real clients POST
application/json).

Run: python manage.py test apps.store.tests.test_staff
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .test_tenant_isolation import TenantFixtureMixin

User = get_user_model()

STAFF = "/api/v1/staff/"


class StaffApiTests(TenantFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # user_a / user_b are owners (role defaults to OWNER). Add an employee
        # in store A to prove the owner-only gate.
        cls.emp_a = User.objects.create_user(
            "emp_a", password="x", store=cls.ph_a, role="employee"
        )

    def setUp(self):
        super().setUp()
        self.EMP = APIClient()
        self.EMP.force_authenticate(self.emp_a)

    # ── access ────────────────────────────────────────────────────────────
    def test_employee_cannot_reach_staff_api(self):
        self.assertEqual(self.EMP.get(STAFF).status_code, 403)
        self.assertEqual(
            self.EMP.post(
                STAFF, {"username": "z", "password": "zzzz"}, format="json"
            ).status_code,
            403,
        )

    def test_owner_lists_only_own_pharmacy(self):
        res = self.A.get(STAFF)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        rows = body["results"] if isinstance(body, dict) else body
        usernames = {r["username"] for r in rows}
        self.assertIn("staff_a", usernames)
        self.assertIn("emp_a", usernames)
        self.assertNotIn("staff_b", usernames)  # store B — never visible
        for r in rows:
            self.assertEqual(User.objects.get(pk=r["id"]).store_id, self.ph_a.pk)

    # ── create ────────────────────────────────────────────────────────────
    def test_owner_creates_staff_in_own_pharmacy(self):
        res = self.A.post(
            STAFF,
            {"username": "cashier1", "password": "secret", "role": "employee"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        u = User.objects.get(username="cashier1", store=self.ph_a)
        self.assertEqual(u.role, "employee")
        self.assertTrue(u.check_password("secret"))
        self.assertNotIn("password", res.json())  # never echoed back

    def test_create_ignores_client_supplied_pharmacy(self):
        # store is stamped from the requester, never trusted from the body.
        res = self.A.post(
            STAFF,
            {"username": "cashier2", "password": "secret", "store": self.ph_b.pk},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(
            User.objects.get(username="cashier2").store_id, self.ph_a.pk
        )

    def test_create_requires_password(self):
        res = self.A.post(STAFF, {"username": "nopass"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_username_unique_within_pharmacy(self):
        res = self.A.post(
            STAFF, {"username": "staff_a", "password": "secret"}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("username", res.json())

    # ── cross-tenant isolation ────────────────────────────────────────────
    def test_cannot_read_or_touch_other_pharmacys_user(self):
        self.assertEqual(self.A.get(f"{STAFF}{self.user_b.pk}/").status_code, 404)
        self.assertEqual(
            self.A.patch(
                f"{STAFF}{self.user_b.pk}/", {"display_name": "hacked"}, format="json"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.A.post(
                f"{STAFF}{self.user_b.pk}/reset-password/",
                {"password": "pwned123"},
                format="json",
            ).status_code,
            404,
        )
        self.user_b.refresh_from_db()
        self.assertNotEqual(self.user_b.display_name, "hacked")
        self.assertTrue(self.user_b.check_password("x"))  # password untouched

    # ── privilege escalation ──────────────────────────────────────────────
    def test_cannot_grant_modules_beyond_pharmacy_tier(self):
        self.ph_a.enabled_modules = ["pos"]  # tenant only bought POS
        self.ph_a.save(update_fields=["enabled_modules"])
        bad = self.A.post(
            STAFF,
            {
                "username": "c3",
                "password": "secret",
                "allowed_modules": ["reports"],  # not in the store tier
            },
            format="json",
        )
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(User.objects.filter(username="c3").exists())  # not created
        good = self.A.post(
            STAFF,
            {"username": "c4", "password": "secret", "allowed_modules": ["pos"]},
            format="json",
        )
        self.assertEqual(good.status_code, 201, good.content)
        self.assertEqual(User.objects.get(username="c4").allowed_modules, ["pos"])

    # ── owner-lockout guards ──────────────────────────────────────────────
    def test_cannot_demote_or_deactivate_last_owner(self):
        # user_a is the sole owner of store A.
        self.assertEqual(
            self.A.patch(
                f"{STAFF}{self.user_a.pk}/", {"role": "employee"}, format="json"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.A.patch(
                f"{STAFF}{self.user_a.pk}/", {"is_active": False}, format="json"
            ).status_code,
            400,
        )
        self.user_a.refresh_from_db()
        self.assertEqual(self.user_a.role, "owner")
        self.assertTrue(self.user_a.is_active)

    def test_can_demote_owner_when_another_owner_remains(self):
        User.objects.create_user(
            "owner2", password="x", store=self.ph_a, role="owner"
        )
        o2 = User.objects.get(username="owner2")
        res = self.A.patch(
            f"{STAFF}{o2.pk}/", {"role": "employee"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        o2.refresh_from_db()
        self.assertEqual(o2.role, "employee")

    # ── reset password ────────────────────────────────────────────────────
    def test_owner_resets_staff_password(self):
        res = self.A.post(
            f"{STAFF}{self.emp_a.pk}/reset-password/",
            {"password": "brandnew"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.emp_a.refresh_from_db()
        self.assertTrue(self.emp_a.check_password("brandnew"))

    def test_reset_password_rejects_too_short(self):
        res = self.A.post(
            f"{STAFF}{self.emp_a.pk}/reset-password/",
            {"password": "1"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    # ── no hard delete ────────────────────────────────────────────────────
    def test_hard_delete_disabled(self):
        res = self.A.delete(f"{STAFF}{self.emp_a.pk}/")
        self.assertEqual(res.status_code, 405)
        self.assertTrue(User.objects.filter(pk=self.emp_a.pk).exists())

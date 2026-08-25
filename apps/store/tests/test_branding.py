"""Owner-only store branding — PATCH /api/v1/store/branding/.

Asserts: owner can set name + upload a logo; employees can't; tenant identity
comes from the user (no-store → 400); an empty request is rejected.

Run: python manage.py test apps.store.tests.test_branding
"""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from .test_tenant_isolation import TenantFixtureMixin

User = get_user_model()

BRANDING = "/api/v1/store/branding/"

# A tiny valid-enough PNG payload; store_upload just persists bytes (no Pillow
# validation on this path), so the exact content doesn't matter.
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 16


class PharmacyBrandingTests(TenantFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.emp_a = User.objects.create_user(
            "brand_emp", password="x", store=cls.ph_a, role="employee"
        )

    def setUp(self):
        super().setUp()
        self.EMP = APIClient()
        self.EMP.force_authenticate(self.emp_a)

    def test_owner_updates_name(self):
        res = self.A.patch(
            BRANDING, {"name": "صيدلية الرحمة الجديدة"}, format="multipart"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.ph_a.refresh_from_db()
        self.assertEqual(self.ph_a.name, "صيدلية الرحمة الجديدة")
        # Never touches the other tenant.
        self.ph_b.refresh_from_db()
        self.assertEqual(self.ph_b.name, "صيدلية النور")

    def test_owner_uploads_logo(self):
        img = SimpleUploadedFile("logo.png", PNG, content_type="image/png")
        res = self.A.patch(BRANDING, {"logo_file": img}, format="multipart")
        self.assertEqual(res.status_code, 200, res.content)
        self.ph_a.refresh_from_db()
        self.assertTrue(self.ph_a.logo)  # a stored value (b2:// marker or media url)
        self.assertTrue(res.json().get("logo"))  # a resolved, servable URL

    def test_employee_forbidden(self):
        res = self.EMP.patch(BRANDING, {"name": "x"}, format="multipart")
        self.assertEqual(res.status_code, 403)
        self.ph_a.refresh_from_db()
        self.assertEqual(self.ph_a.name, "صيدلية الرحمة")  # unchanged

    def test_empty_request_rejected(self):
        res = self.A.patch(BRANDING, {}, format="multipart")
        self.assertEqual(res.status_code, 400)

    def test_no_pharmacy_is_400(self):
        # user_none has no store → the tenant guard rejects with 400.
        res = self.N.patch(BRANDING, {"name": "x"}, format="multipart")
        self.assertEqual(res.status_code, 400)

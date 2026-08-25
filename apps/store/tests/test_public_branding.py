"""Public per-tenant branding endpoints — name + logo only, tenant-safe.

Run: python manage.py test apps.store.tests.test_public_branding
"""
import io
import tempfile

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.store import models


def _png_bytes(size=(64, 40), color=(30, 120, 200, 255)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="branding-test-media-"))
class PublicBrandingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client_ = APIClient()
        self.active = models.Store.objects.create(
            name="صيدلية الحياة", slug="br-active", logo="https://cdn.example/logo.png"
        )
        self.no_logo = models.Store.objects.create(name="بلا شعار", slug="br-bare")
        self.inactive = models.Store.objects.create(
            name="موقوفة", slug="br-off", is_active=False, logo="https://x/l.png"
        )

    def _get(self, path, **params):
        cache.clear()
        return self.client_.get(path, params)

    def test_branding_returns_name_and_logo_only(self):
        r = self._get("/api/v1/public/branding/", store="br-active")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.json(), {"name": "صيدلية الحياة", "logo": "https://cdn.example/logo.png"}
        )

    def test_branding_empty_logo_passes_through(self):
        r = self._get("/api/v1/public/branding/", store="br-bare")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["logo"], "")

    def test_branding_unknown_or_inactive_404s(self):
        for slug in ("nope", "br-off"):
            r = self._get("/api/v1/public/branding/", store=slug)
            self.assertEqual(r.status_code, 404, slug)
        # MISSING slug is a different contract: the tenant-API guard answers
        # 400 "store_id is required" before the view runs.
        for params in ({}, {"store": ""}):
            r = self._get("/api/v1/public/branding/", **params)
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json(), {"detail": "store_id is required"})

    def test_branding_never_exposes_extra_fields(self):
        r = self._get("/api/v1/public/branding/", store="br-active")
        self.assertEqual(set(r.json().keys()), {"name", "logo"})

    def test_icon_404_when_no_logo(self):
        r = self._get("/api/v1/public/branding/icon/", store="br-bare")
        self.assertEqual(r.status_code, 404)
        r = self._get("/api/v1/public/branding/icon/", store="br-off")
        self.assertEqual(r.status_code, 404)

    def test_icon_resizes_local_media_logo(self):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        from PIL import Image

        name = default_storage.save("logos/test-logo.png", ContentFile(_png_bytes()))
        self.active.logo = f"/media/{name}"
        self.active.save()

        for size, maskable in ((192, ""), (512, ""), (192, "1")):
            r = self._get(
                "/api/v1/public/branding/icon/",
                store="br-active",
                size=str(size),
                maskable=maskable,
            )
            self.assertEqual(r.status_code, 200, (size, maskable))
            self.assertEqual(r["Content-Type"], "image/png")
            img = Image.open(io.BytesIO(r.content))
            self.assertEqual(img.size, (size, size))
        default_storage.delete(name)

    def test_icon_rejects_weird_sizes_gracefully(self):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        from PIL import Image

        name = default_storage.save("logos/test-logo2.png", ContentFile(_png_bytes()))
        self.active.logo = f"/media/{name}"
        self.active.save()
        r = self._get(
            "/api/v1/public/branding/icon/", store="br-active", size="9999"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Image.open(io.BytesIO(r.content)).size, (192, 192))
        default_storage.delete(name)

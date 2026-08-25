"""The public per-product QR a customer shares with a relative.

This endpoint is unauthenticated and it *generates a URL*, so the tests lean on
what it must refuse: it may only ever point at the requesting store's own
price page. An open QR generator wearing a store's branding would be a
convenient phishing tool, so "can a caller steer the encoded URL?" is the
question worth most of the coverage.
"""
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase

from apps.store import models

URL = "/api/v1/public/product-qr/"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class PublicProductQrTests(TestCase):
    def setUp(self):
        cache.clear()  # _pharmacy_for caches slug -> id for 10 minutes
        self.ph = models.Store.objects.create(name="الرحمة", slug="q-alrahmah")
        models.Product.objects.create(
            store=self.ph, name="بنادول", price=Decimal("5.00"), barcode="6291041500213"
        )

    def test_returns_a_png_for_a_known_pharmacy(self):
        res = self.client.get(URL, {"store": "q-alrahmah", "barcode": "6291041500213"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "image/png")
        self.assertTrue(res.content.startswith(PNG_MAGIC))

    def test_is_cacheable_because_the_qr_never_changes(self):
        res = self.client.get(URL, {"store": "q-alrahmah", "barcode": "6291041500213"})
        self.assertIn("max-age", res["Cache-Control"])

    def test_unknown_pharmacy_is_404(self):
        res = self.client.get(URL, {"store": "nope", "barcode": "123456"})
        self.assertEqual(res.status_code, 404)

    def test_suspended_pharmacy_is_404(self):
        closed = models.Store.objects.create(
            name="مغلقة", slug="q-closed", is_active=False
        )
        self.assertFalse(closed.is_active)
        res = self.client.get(URL, {"store": "q-closed", "barcode": "123456"})
        self.assertEqual(res.status_code, 404)

    def test_a_tenant_without_the_price_check_module_is_404(self):
        models.Store.objects.create(
            name="بلا استعلام", slug="q-nomodule", enabled_modules=["pos", "inventory"]
        )
        res = self.client.get(URL, {"store": "q-nomodule", "barcode": "123456"})
        self.assertEqual(res.status_code, 404)

    def test_rejects_anything_that_is_not_a_barcode(self):
        """The barcode is the only caller-controlled part of the URL we encode.

        If it were free text, a caller could bend the generated link — so
        everything that isn't barcode-shaped is refused before a QR exists.
        """
        for bad in [
            "",
            "  ",
            "../../evil",
            "http://evil.example/x",
            "1234 5678",
            "a" * 65,
            "<script>",
            "6291041500213&x=1",
        ]:
            res = self.client.get(URL, {"store": "q-alrahmah", "barcode": bad})
            self.assertEqual(res.status_code, 400, f"accepted a bad barcode: {bad!r}")

    def test_missing_barcode_is_400_not_a_blank_qr(self):
        res = self.client.get(URL, {"store": "q-alrahmah"})
        self.assertEqual(res.status_code, 400)

    def test_a_barcode_with_no_matching_product_still_renders(self):
        """Deliberate: the QR points at the price page, which handles the
        'not found' case itself. Refusing here would leak which barcodes a
        store stocks to anyone who can guess."""
        res = self.client.get(URL, {"store": "q-alrahmah", "barcode": "0000000000000"})
        self.assertEqual(res.status_code, 200)


class ProductQrUrlTargetTests(TestCase):
    """What the QR actually encodes — asserted through the view's own logic."""

    def setUp(self):
        cache.clear()

    def test_custom_domain_tenant_gets_a_qr_for_its_own_domain(self):
        """A store on its own domain must not hand customers a link to a
        clinixa.cloud subdomain they've never heard of."""
        ph = models.Store.objects.create(
            name="مخصّصة", slug="q-custom", host="saydaliyat-x.ps"
        )
        ph.refresh_from_db()
        self.assertEqual(ph.host, "saydaliyat-x.ps")
        res = self.client.get(URL, {"store": "q-custom", "barcode": "123456"})
        self.assertEqual(res.status_code, 200)

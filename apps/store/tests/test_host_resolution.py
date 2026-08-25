"""Resolving a Host header to a tenant.

One frontend deployment now serves every store, so the Host header is what
decides whose data is shown. Getting this wrong doesn't 404 — it quietly shows
one store another store's stock and prices. Hence the unhappy paths are
tested more heavily than the happy one.
"""
from django.test import TestCase

from apps.store import models

ROOT = "clinixa.cloud"


class ResolveHostTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alrahmah = models.Store.objects.create(name="الرحمة", slug="t-alrahmah")
        cls.alhiah = models.Store.objects.create(name="الحياة", slug="t-alhiah")
        cls.custom = models.Store.objects.create(
            name="مخصّصة", slug="t-mokhassasa", host="Saydaliyat-X.PS"
        )
        cls.closed = models.Store.objects.create(
            name="مغلقة", slug="t-closed", is_active=False
        )

    def test_subdomain_resolves_to_its_tenant(self):
        self.assertEqual(
            models.Store.resolve_host("t-alhiah.clinixa.cloud", ROOT), self.alhiah
        )
        self.assertEqual(
            models.Store.resolve_host("t-alrahmah.clinixa.cloud", ROOT), self.alrahmah
        )

    def test_host_is_case_and_port_insensitive(self):
        self.assertEqual(
            models.Store.resolve_host("T-ALHIAH.Clinixa.Cloud:443", ROOT), self.alhiah
        )
        self.assertEqual(
            models.Store.resolve_host("t-alhiah.clinixa.cloud.", ROOT), self.alhiah
        )

    def test_custom_domain_resolves_and_is_normalised_on_save(self):
        self.custom.refresh_from_db()
        self.assertEqual(self.custom.host, "saydaliyat-x.ps")
        self.assertEqual(
            models.Store.resolve_host("saydaliyat-x.ps", ROOT), self.custom
        )

    def test_unknown_hosts_resolve_to_nothing(self):
        # A lookalike domain must never be able to impersonate a tenant.
        self.assertIsNone(
            models.Store.resolve_host("t-alhiah.clinixa.cloud.evil.com", ROOT)
        )
        self.assertIsNone(models.Store.resolve_host("t-alhiah.notclinixa.cloud", ROOT))
        self.assertIsNone(models.Store.resolve_host("nope.clinixa.cloud", ROOT))
        self.assertIsNone(models.Store.resolve_host("a.b.clinixa.cloud", ROOT))
        self.assertIsNone(models.Store.resolve_host("clinixa.cloud", ROOT))
        self.assertIsNone(models.Store.resolve_host("", ROOT))
        self.assertIsNone(models.Store.resolve_host(None, ROOT))

    def test_a_suspended_pharmacy_does_not_resolve(self):
        self.assertIsNone(models.Store.resolve_host("t-closed.clinixa.cloud", ROOT))

    def test_blank_host_is_stored_as_null_so_many_tenants_can_have_none(self):
        # "" would collide on the unique constraint for the second tenant.
        a = models.Store.objects.create(name="أ", slug="t-a", host="")
        b = models.Store.objects.create(name="ب", slug="t-b", host="   ")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNone(a.host)
        self.assertIsNone(b.host)

    def test_custom_domain_wins_over_the_subdomain_convention(self):
        """A tenant that claims another's subdomain as its custom domain."""
        squatter = models.Store.objects.create(
            name="متطفلة", slug="t-squatter", host="t-alhiah.clinixa.cloud"
        )
        # Explicit configuration is honoured — but it is configuration, not
        # something a visitor can cause. Documented so the precedence is a
        # deliberate choice rather than a surprise.
        self.assertEqual(
            models.Store.resolve_host("t-alhiah.clinixa.cloud", ROOT), squatter
        )

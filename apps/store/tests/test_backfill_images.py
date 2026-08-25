"""Phase B backfill (migration 0025): shared CatalogItem photos are copied down
into each store's own Product rows — additively and idempotently.

Run: python manage.py test apps.store.tests.test_backfill_images
"""
from decimal import Decimal
from importlib import import_module

from django.apps import apps as django_apps
from django.test import TestCase

from apps.store import models

copy_images_down = import_module(
    "apps.store.migrations.0025_copy_product_images_to_medications"
).copy_images_down


class BackfillProductImagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph_a = models.Store.objects.create(name="A", slug="bf-a")
        cls.ph_b = models.Store.objects.create(name="B", slug="bf-b")
        cls.catalog_item = models.CatalogItem.objects.create(
            barcode="111", name="Shared", image="https://cdn.example/shared.png"
        )
        models.CatalogItemImage.objects.create(
            product=cls.catalog_item, image="https://cdn.example/g1.png", position=1
        )
        models.CatalogItemImage.objects.create(
            product=cls.catalog_item, image="https://cdn.example/g2.png", position=2
        )

    def _run(self):
        copy_images_down(django_apps, None)

    def test_empty_image_gets_the_shared_default_copied_in(self):
        med = models.Product.objects.create(
            store=self.ph_a, product=self.catalog_item, name="M", barcode="111",
            price=Decimal("5"), image="",
        )
        self._run()
        med.refresh_from_db()
        self.assertEqual(med.image, "https://cdn.example/shared.png")

    def test_own_image_is_never_overwritten(self):
        med = models.Product.objects.create(
            store=self.ph_b, product=self.catalog_item, name="M", barcode="111",
            price=Decimal("5"), image="https://cdn.example/own.png",
        )
        self._run()
        med.refresh_from_db()
        self.assertEqual(med.image, "https://cdn.example/own.png")

    def test_gallery_copied_to_every_linked_medication(self):
        med_a = models.Product.objects.create(
            store=self.ph_a, product=self.catalog_item, name="MA", barcode="111",
            price=Decimal("5"),
        )
        med_b = models.Product.objects.create(
            store=self.ph_b, product=self.catalog_item, name="MB", barcode="111",
            price=Decimal("9"),
        )
        # B already carries g1 — must not be duplicated.
        models.ProductImage.objects.create(
            product=med_b, image="https://cdn.example/g1.png", position=7
        )
        self._run()
        self.assertEqual(
            list(med_a.images.values_list("image", "position")),
            [("https://cdn.example/g1.png", 1), ("https://cdn.example/g2.png", 2)],
        )
        self.assertEqual(med_b.images.count(), 2)  # g1 (existing) + g2 (copied)
        self.assertEqual(
            med_b.images.filter(image="https://cdn.example/g1.png").count(), 1
        )

    def test_unlinked_medication_untouched(self):
        med = models.Product.objects.create(
            store=self.ph_a, name="Solo", barcode="999", price=Decimal("5"),
        )
        self._run()
        med.refresh_from_db()
        self.assertEqual(med.image, "")
        self.assertEqual(med.images.count(), 0)

    def test_idempotent_second_run_changes_nothing(self):
        med = models.Product.objects.create(
            store=self.ph_a, product=self.catalog_item, name="M", barcode="111",
            price=Decimal("5"),
        )
        self._run()
        self._run()
        med.refresh_from_db()
        self.assertEqual(med.image, "https://cdn.example/shared.png")
        self.assertEqual(med.images.count(), 2)
        self.assertEqual(
            models.ProductImage.objects.unscoped().filter(product=med).count(), 2
        )

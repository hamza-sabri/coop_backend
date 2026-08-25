"""Multi-image gallery: main + ordered secondary photos, tenant-safe."""
import io

from django.contrib.auth import get_user_model
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()


def png(name="x.png", color="red"):
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, "PNG")
    buf.seek(0)
    buf.name = name
    return buf


class MedicationGalleryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ph = models.Store.objects.create(name="A", slug="gal-a")
        cls.other = models.Store.objects.create(name="B", slug="gal-b")
        cls.user = User.objects.create_user("staff", password="x", store=cls.ph)
        cls.user_b = User.objects.create_user("staff_b", password="x", store=cls.other)

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_create_with_main_and_secondary_images(self):
        r = self.api.post(
            "/api/v1/products/",
            {"name": "Med", "price": "5", "image_file": png("main.png"),
             "image_files": [png("a.png"), png("b.png")]},
            format="multipart",
        )
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertTrue(body["image"])                 # main
        self.assertEqual(len(body["images"]), 2)       # gallery, ordered
        self.assertTrue(all(im["url"] for im in body["images"]))
        med = models.Product.objects.unscoped().get(pk=body["id"])
        self.assertEqual(med.images.count(), 2)

    def test_add_then_remove_secondary_images(self):
        r = self.api.post(
            "/api/v1/products/",
            {"name": "Med", "price": "5", "image_files": [png("a.png")]},
            format="multipart",
        )
        med_id = r.json()["id"]
        first_id = r.json()["images"][0]["id"]
        # add one more
        r = self.api.patch(
            f"/api/v1/products/{med_id}/",
            {"image_files": [png("b.png")]},
            format="multipart",
        )
        self.assertEqual(len(r.json()["images"]), 2)
        # order is stable: first uploaded stays first
        self.assertEqual(r.json()["images"][0]["id"], first_id)
        # remove the first
        r = self.api.patch(
            f"/api/v1/products/{med_id}/",
            {"remove_images": str(first_id)},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["images"]), 1)
        self.assertNotEqual(r.json()["images"][0]["id"], first_id)

    def test_gallery_cap_enforced(self):
        files = [png(f"{i}.png") for i in range(9)]
        r = self.api.post(
            "/api/v1/products/",
            {"name": "Med", "price": "5", "image_files": files},
            format="multipart",
        )
        self.assertEqual(r.status_code, 400)

    def test_remove_images_cannot_touch_another_meds_or_tenants_photos(self):
        mine = self.api.post(
            "/api/v1/products/",
            {"name": "Mine", "price": "5", "image_files": [png()]},
            format="multipart",
        ).json()
        # another med in ANOTHER store with a photo
        theirs_med = models.Product.objects.create(
            store=self.other, name="Theirs", price=5
        )
        theirs_img = models.ProductImage.objects.create(
            product=theirs_med, image="https://example.com/x.png"
        )
        # try to delete their image id through MY med
        r = self.api.patch(
            f"/api/v1/products/{mine['id']}/",
            {"remove_images": str(theirs_img.id)},
            format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(
            models.ProductImage.objects.unscoped().filter(pk=theirs_img.pk).exists()
        )

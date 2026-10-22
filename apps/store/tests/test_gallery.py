"""Extra pictures per drink: up to five beside the main one, and the
customer app's menu lists them in order."""
import io

from django.contrib.auth import get_user_model
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from apps.store import models

User = get_user_model()


def jpeg(name):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 120, 40)).save(buf, "JPEG")
    buf.seek(0)
    buf.name = name
    return buf


class Gallery(TestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="A", slug="gal-a")
        self.api = APIClient()
        self.api.force_authenticate(User.objects.create_user("g", password="x", store=self.store))

    def test_five_extra_pictures_at_most(self):
        r = self.api.post("/api/v1/products/", {"name": "G", "price": "5",
                                                "image_files": [jpeg(f"{i}.jpg") for i in range(6)]}, format="multipart")
        self.assertEqual(r.status_code, 400)
        r = self.api.post("/api/v1/products/", {"name": "G", "price": "5",
                                                "image_files": [jpeg(f"{i}.jpg") for i in range(5)]}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(len(r.json()["images"]), 5)

    def test_public_menu_lists_them_in_order(self):
        p = models.Product.objects.create(store=self.store, name="L", price=5, image="https://cdn.example.com/main.jpg")
        models.ProductImage.objects.create(product=p, image="https://cdn.example.com/b.jpg", position=2)
        models.ProductImage.objects.create(product=p, image="https://cdn.example.com/a.jpg", position=1)
        item = APIClient().get("/api/v1/public/menu/?store=gal-a").json()["items"][0]
        self.assertEqual([u.split("#")[0] for u in item["images"]],
                         ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"])

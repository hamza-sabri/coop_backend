"""Blurhash previews: computed on upload, served in the URL fragment, never
stored back, backfilled by a command, and never able to break a request."""
import io
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from apps.core import blurhash
from apps.core.models import ImageHash
from apps.store import models

User = get_user_model()


def jpeg(name="x.jpg", color=(200, 120, 40), size=(40, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    buf.seek(0)
    buf.name = name
    return buf


def code_in(url: str) -> str | None:
    return url.split("#bh=", 1)[1] if "#bh=" in url else None


class Encoder(TestCase):
    def test_matches_reference_vectors(self):
        # Checked against the reference implementation (pip "blurhash").
        import random

        random.seed(1)
        px = [(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) for _ in range(32 * 24)]
        self.assertEqual(blurhash.encode_pixels(px, 32, 24, 4, 3), "L6HeqM|8t]?Jt.bpxnw5#YK[NXIv")

    def test_unreadable_bytes_give_none(self):
        self.assertIsNone(blurhash.hash_bytes(b"not an image"))

    def test_identity_is_the_same_for_stored_and_signed_forms(self):
        name = "a" * 32 + ".jpg"
        self.assertEqual(
            blurhash.identity(f"b2://products/{name}"),
            blurhash.identity(f"https://bucket.s3.eu.backblazeb2.com/products/{name}?X-Amz-Signature=x"),
        )
        self.assertEqual(blurhash.identity(f"/media/products/{name}"), blurhash.identity(f"b2://products/{name}"))
        self.assertIsNone(blurhash.identity("data:image/png;base64,xx"))
        self.assertTrue(blurhash.identity("https://cdn.example.com/p/latte.jpg").startswith("url:"))


class Flow(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.store = models.Store.objects.create(name="A", slug="bh-a")
        cls.user = User.objects.create_user("bh-staff", password="x", store=cls.store)

    def setUp(self):
        blurhash.reset_memory()
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_upload_is_hashed_and_served_in_the_fragment(self):
        r = self.api.post("/api/v1/products/", {"name": "Latte", "price": "5", "image_file": jpeg()}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(code_in(r.json()["image"]), r.json()["image"])
        self.assertEqual(ImageHash.objects.count(), 1)
        p = models.Product.objects.for_pharmacy(self.store).get()
        self.assertNotIn("#bh=", p.image)  # stored clean
        # Served again after a restart (memory empty): still there.
        blurhash.reset_memory()
        r = self.api.get(f"/api/v1/products/{p.id}/")
        from urllib.parse import unquote

        self.assertEqual(unquote(code_in(r.json()["image"])), ImageHash.objects.get().blurhash)

    def test_url_echoed_back_is_stored_without_the_fragment(self):
        url = "https://cdn.example.com/p/latte.jpg"
        ImageHash.objects.create(key=blurhash.identity(url), blurhash="LKO2?U%2Tw=w]~RBVZRi};RPxuwH")
        p = models.Product.objects.create(store=self.store, name="Mocha", price=4, image=url)
        r = self.api.get(f"/api/v1/products/{p.id}/")
        served = r.json()["image"]
        self.assertTrue(served.startswith(url + "#bh="), served)
        r = self.api.patch(f"/api/v1/products/{p.id}/", {"image": served}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        p.refresh_from_db()
        self.assertEqual(p.image, url)

    def test_unknown_picture_is_queued_not_fetched_inline(self):
        p = models.Product.objects.create(store=self.store, name="Tea", price=3, image="https://cdn.example.com/tea.jpg")
        with override_settings(BLURHASH_BACKGROUND=True), mock.patch.object(blurhash, "_start_worker") as start, \
                mock.patch.object(blurhash, "fetch_bytes") as fetch:
            r = self.api.get(f"/api/v1/products/{p.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("#bh=", r.json()["image"])
        start.assert_called()
        fetch.assert_not_called()
        # Videos are never queued.
        with override_settings(BLURHASH_BACKGROUND=True), mock.patch.object(blurhash, "_start_worker") as start2:
            blurhash.enqueue("https://cdn.example.com/clip.mp4")
        start2.assert_not_called()

    def test_backfill_command_hashes_what_is_missing(self):
        models.Product.objects.create(store=self.store, name="A", price=1, image="https://cdn.example.com/a.jpg")
        models.Product.objects.create(store=self.store, name="B", price=1, image="https://cdn.example.com/b.jpg")
        self.store.logo = "https://cdn.example.com/logo.png"
        self.store.save()
        data = jpeg().getvalue()
        with mock.patch.object(blurhash, "fetch_bytes", return_value=data) as fetch:
            call_command("blurhash_images", stdout=io.StringIO())
            self.assertEqual(fetch.call_count, 3)
            call_command("blurhash_images", stdout=io.StringIO())  # re-run: nothing to do
            self.assertEqual(fetch.call_count, 3)
        self.assertEqual(ImageHash.objects.count(), 3)

    def test_a_broken_hash_table_never_breaks_a_response(self):
        p = models.Product.objects.create(store=self.store, name="X", price=1, image="https://cdn.example.com/x.jpg")
        with mock.patch.object(blurhash, "lookup", side_effect=RuntimeError("boom")):
            r = self.api.get(f"/api/v1/products/{p.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["image"], "https://cdn.example.com/x.jpg")


class SmallCopies(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.store = models.Store.objects.create(name="T", slug="bh-t")
        cls.user = User.objects.create_user("bh-t", password="x", store=cls.store)

    def setUp(self):
        blurhash.reset_memory()
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_big_upload_is_served_as_a_small_webp(self):
        big = jpeg("big.jpg", size=(2268, 4032))
        r = self.api.post("/api/v1/products/", {"name": "Big", "price": "5", "image_file": big}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        served = r.json()["image"]
        self.assertIn("/thumbs/", served)
        self.assertIn(".webp#bh=", served)
        p = models.Product.objects.for_pharmacy(self.store).get()
        self.assertNotIn("thumbs", p.image)  # the original is what is stored
        from django.core.files.storage import default_storage

        rel = served.split("/media/", 1)[1].split("#", 1)[0]
        with default_storage.open(rel) as f:
            im = Image.open(f)
            self.assertEqual(im.format, "WEBP")
            self.assertLessEqual(max(im.size), blurhash.THUMB_PX)

    def test_small_upload_is_served_as_it_is(self):
        r = self.api.post("/api/v1/products/", {"name": "Small", "price": "5", "image_file": jpeg()}, format="multipart")
        self.assertNotIn("/thumbs/", r.json()["image"])
        self.assertEqual(ImageHash.objects.get().thumb, blurhash.NO_THUMB)

    def test_thumb_keeps_transparency(self):
        buf = io.BytesIO()
        Image.new("RGBA", (1600, 1600), (255, 0, 0, 0)).save(buf, "PNG")
        out = blurhash.thumb_bytes(buf.getvalue())
        self.assertEqual(Image.open(io.BytesIO(out)).mode, "RGBA")

    def test_existing_rows_without_a_copy_are_redone_by_the_command(self):
        url = "https://cdn.example.com/old.jpg"
        models.Product.objects.create(store=self.store, name="Old", price=1, image=url)
        ImageHash.objects.create(key=blurhash.identity(url), blurhash="LKO2?U%2Tw=w]~RBVZRi};RPxuwH", thumb="")
        with mock.patch.object(blurhash, "fetch_bytes", return_value=jpeg(size=(1600, 1200)).getvalue()) as fetch, \
                mock.patch.object(blurhash, "save_thumb", return_value="b2://thumbs/x.webp"):
            call_command("blurhash_images", stdout=io.StringIO())
        fetch.assert_called_once()
        self.assertEqual(ImageHash.objects.get().thumb, "b2://thumbs/x.webp")

    @override_settings(STORAGE_ENABLED=True, B2_BUCKET_NAME="coop-media")
    def test_signed_link_echoed_back_is_never_stored(self):
        p = models.Product.objects.create(store=self.store, name="S", price=1, image="b2://products/" + "a" * 32 + ".jpg")
        # Our small copy, signed, echoed back by a client: the original stays.
        p.image = "https://s3.eu-central-003.backblazeb2.com/coop-media/thumbs/" + "b" * 32 + ".webp?X-Amz-Signature=x#bh=abc"
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.image, "b2://products/" + "a" * 32 + ".jpg")
        # Any other signed link becomes its stable stored form.
        p.image = "https://s3.eu-central-003.backblazeb2.com/coop-media/products/" + "c" * 32 + ".jpg?X-Amz-Signature=y"
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.image, "b2://products/" + "c" * 32 + ".jpg")

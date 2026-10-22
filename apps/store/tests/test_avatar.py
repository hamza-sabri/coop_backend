"""The customer changes their own photo from the app."""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.store import models
from apps.store.tests.test_app_auth import Base


def jpeg(w=900, h=600, color=(200, 120, 40)) -> SimpleUploadedFile:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return SimpleUploadedFile("me.jpg", buf.getvalue(), content_type="image/jpeg")


class Avatar(Base):
    def signed_in(self):
        self.otp()
        return self.as_customer(self.verify().json()["token"])

    def test_upload_is_squared_and_saved_on_the_customer(self):
        api = self.signed_in()
        r = api.post("/api/v1/shop/me/avatar/", {"file": jpeg()}, format="multipart")
        self.assertEqual(r.status_code, 200, r.content)
        url = r.json()["avatar"]
        self.assertTrue(url)
        c = models.Customer.objects.for_pharmacy(self.a).get()
        self.assertTrue(c.avatar)
        self.assertEqual(api.get("/api/v1/shop/me/").json()["profile"]["avatar"], url)

        # stored square, 512px, as JPEG
        from django.core.files.storage import default_storage
        name = c.avatar.split("/media/", 1)[-1] if "/media/" in c.avatar else c.avatar.split("://", 1)[-1]
        with default_storage.open(name) as fh:
            img = Image.open(fh)
            self.assertEqual(img.size, (512, 512))
            self.assertEqual(img.format, "JPEG")

    def test_not_an_image_is_refused(self):
        api = self.signed_in()
        bad = SimpleUploadedFile("x.jpg", b"not really a picture", content_type="image/jpeg")
        self.assertEqual(api.post("/api/v1/shop/me/avatar/", {"file": bad}, format="multipart").status_code, 400)
        self.assertEqual(api.post("/api/v1/shop/me/avatar/", {}, format="multipart").status_code, 400)

    def test_remove_and_signed_out(self):
        api = self.signed_in()
        api.post("/api/v1/shop/me/avatar/", {"file": jpeg()}, format="multipart")
        r = api.delete("/api/v1/shop/me/avatar/")
        self.assertEqual(r.json(), {"avatar": ""})
        self.assertEqual(models.Customer.objects.for_pharmacy(self.a).get().avatar, "")
        from rest_framework.test import APIClient
        self.assertIn(APIClient().post("/api/v1/shop/me/avatar/", {"file": jpeg()}, format="multipart").status_code,
                      (401, 403))

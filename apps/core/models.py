from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base with created/updated timestamps.

    Inherit this in your models to get consistent auditing for free:

        class CatalogItem(TimeStampedModel):
            name = models.CharField(max_length=200)
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created_at"]


class ImageHash(models.Model):
    """Blurhash of one picture (see apps/core/blurhash.py).

    `key` is the picture's identity — "f:<uuid>.<ext>" for our own uploads,
    "url:<sha1>" for an outside URL. Not tenant data: a code is a few colours.
    """

    key = models.CharField(max_length=80, unique=True)
    blurhash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.key} → {self.blurhash}"

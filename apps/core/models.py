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

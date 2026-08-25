from django.db import migrations, models


class Migration(migrations.Migration):
    """Per-line notes at the till.

    A café order is "two lattes, one without sugar" — the note belongs to the
    drink, not to the order. The sale already snapshots the product name and
    the variant label for exactly this reason: the record has to survive the
    catalogue changing underneath it.
    """

    dependencies = [
        ("store", "0003_category_icon_customer_clerk_id_customer_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="saleitem",
            name="note",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]

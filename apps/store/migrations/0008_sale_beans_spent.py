"""Points spent at the counter, recorded on the sale.

A copy of the REDEEM ledger row, so a receipt reprinted months later shows what
the customer actually paid without walking the ledger.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0007_customer_firebase_uid_devicetoken"),
    ]

    operations = [
        migrations.AddField(
            model_name="sale",
            name="beans_spent",
            field=models.IntegerField(default=0),
        ),
    ]

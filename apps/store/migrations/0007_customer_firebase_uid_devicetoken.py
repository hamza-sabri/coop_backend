"""Firebase identity on Customer, and FCM tokens for the native app.

Hand-written rather than generated, and it is worth saying why: it must match
what `makemigrations` would produce, so run `makemigrations store --check` after
this lands. If that reports pending changes, the model and this file disagree
and the generated version wins.

`firebase_uid` is added blank rather than null for the same reason `clerk_id`
is: the lookups filter on `firebase_uid=""` to mean "not linked yet", and NULL
would make every one of those comparisons silently false.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0006_order_beans_spent_order_fulfilment_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="firebase_uid",
            field=models.CharField(blank=True, db_index=True, max_length=128),
        ),
        migrations.CreateModel(
            name="DeviceToken",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("token", models.TextField(unique=True)),
                (
                    "platform",
                    models.CharField(
                        choices=[("android", "Android"), ("ios", "iOS")],
                        db_index=True,
                        default="android",
                        max_length=10,
                    ),
                ),
                ("device_name", models.CharField(blank=True, max_length=120)),
                ("app_version", models.CharField(blank=True, max_length=40)),
                (
                    "last_seen_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="device_tokens",
                        to="store.customer",
                    ),
                ),
                (
                    "store",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="device_tokens",
                        to="store.store",
                    ),
                ),
            ],
            options={
                "verbose_name": "Device token",
                "verbose_name_plural": "Device tokens",
                "ordering": ["-last_seen_at", "-created_at"],
                "base_manager_name": "unguarded",
                "default_manager_name": "unguarded",
            },
        ),
    ]

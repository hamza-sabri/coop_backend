"""Every customer phone in one local format ("0599123456"), so the app's phone
sign-in (which reports "+970599…") finds the customer the staff registered.
A number that would clash with another customer's is left as it was."""
from django.db import migrations


def run(apps, schema_editor):
    from apps.store.club import normalise_phone

    Customer = apps.get_model("store", "Customer")
    for c in Customer.unguarded.exclude(phone__isnull=True).exclude(phone="").iterator():
        new = normalise_phone(c.phone) or None
        if new == c.phone:
            continue
        if new and Customer.unguarded.filter(store_id=c.store_id, phone=new).exclude(pk=c.pk).exists():
            continue
        c.phone = new
        c.save(update_fields=["phone"])


class Migration(migrations.Migration):
    dependencies = [("store", "0018_category_default_icons")]
    operations = [migrations.RunPython(run, migrations.RunPython.noop)]

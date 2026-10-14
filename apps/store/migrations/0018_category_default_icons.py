from django.db import migrations


def fill(apps, schema_editor):
    from apps.store.category_icons import guess

    Category = apps.get_model("store", "Category")
    for c in Category.unguarded.filter(icon=""):
        c.icon = guess(c.name)
        c.save(update_fields=["icon"])


class Migration(migrations.Migration):
    dependencies = [("store", "0017_customer_profile")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]

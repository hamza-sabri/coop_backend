from django.db import transaction

from . import models


def _taxonomy(model, store, name):
    name = (name or "").strip()
    if not name:
        return None
    return (
        model.objects.for_pharmacy(store).filter(name__iexact=name).first()
        or model.objects.create(store=store, name=name)
    )


def _clone_one(src, target):
    barcode = (src.barcode or "").strip()
    existing = None
    if barcode:
        existing = models.Product.objects.for_pharmacy(target).filter(
            barcode=barcode
        ).first()
    if existing is None:
        existing = models.Product.objects.for_pharmacy(target).filter(
            name=src.name
        ).first()
    med = existing or models.Product(store=target)
    med.name = src.name
    med.barcode = src.barcode
    med.price = src.price
    med.cost = src.cost
    med.stock = src.stock
    med.brand = src.brand
    med.notes = src.notes
    med.image = src.image
    med.attributes = src.attributes
    med.category = _taxonomy(
        models.Category, target, src.category.name if src.category_id else ""
    )
    med.manufacturer = _taxonomy(
        models.Manufacturer,
        target,
        src.manufacturer.name if src.manufacturer_id else "",
    )
    med.save()
    med.variants.all().delete()
    variants = 0
    for v in src.variants.all():
        models.ProductVariant.objects.create(
            product=med,
            label=v.label,
            attributes=v.attributes,
            barcode=v.barcode,
            price=v.price,
            cost=v.cost,
            stock=v.stock,
            image=v.image,
            is_active=v.is_active,
        )
        variants += 1
    if not med.images.exists():
        for im in src.images.all():
            med.images.create(image=im.image, position=im.position)
    return (0 if existing else 1), variants


def clone_catalog(source, target) -> dict:
    created = skipped = variant_count = total = 0
    source_meds = models.Product.objects.for_pharmacy(
        source
    ).prefetch_related("variants", "images")
    for src in source_meds:
        total += 1
        try:
            with transaction.atomic():
                is_new, variants = _clone_one(src, target)
            created += is_new
            variant_count += variants
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            print(f"[clone] skipped med {src.pk} ({src.name!r}): {exc}")
    return {
        "source_total": total,
        "created": created,
        "skipped": skipped,
        "variants": variant_count,
    }

"""Turning a collected app order into a real sale.

Until this existed, an order placed from the app and handed over at the
counter was recorded as an Order and nothing else: no Sale row, so it was
missing from the day's takings, missing from the reports, and — the part the
owner notices — it took nothing out of stock. Twenty app orders of the same
syrup and the inventory still said full.

So collection is the moment the order becomes a sale, with the same shape a
counter sale has: snapshotted lines, a receipt code, a customer, stock moved.
One transaction, keyed on the order, so a barista double-tapping "collected"
produces one sale and moves stock once.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F

log = logging.getLogger(__name__)


def sale_for_order(order, *, created_by=None):
    """Create (or return) the Sale behind a collected order.

    Idempotent: a second call for the same order returns the first sale. The
    link is `Order.sale`, set inside the same transaction that creates it.
    """
    from apps.store import points as points_service
    from apps.store.models import Sale, SaleItem, Product, ProductVariant

    if order.sale_id:
        return order.sale

    with transaction.atomic():
        # Re-read under lock: two staff screens can both show "ready" and both
        # tap "collected" within the same second.
        order = type(order).objects.unscoped().select_for_update().get(pk=order.pk)
        if order.sale_id:
            return order.sale

        sale = None
        for _ in range(4):
            try:
                with transaction.atomic():
                    sale = Sale.objects.create(
                        store=order.store,
                        customer=order.customer,
                        payment_method="cash",
                        is_return=False,
                        receipt_code=Sale.new_receipt_code(),
                        note=order.note or "",
                        created_by=created_by,
                        # The same idempotency key as the order carried, so a
                        # sync of the app's outbox and this both land on one row.
                        client_uuid=f"order:{order.pk}",
                    )
                break
            except IntegrityError:
                sale = None
        if sale is None:
            raise RuntimeError(f"could not mint a receipt code for order {order.pk}")

        for line in order.items.all():
            product = line.product
            variant = line.variant
            SaleItem.objects.create(
                sale=sale,
                product=product,
                variant=variant,
                medication_name=line.name,
                variant_label=getattr(variant, "label", "") or "",
                note=line.note or "",
                category=(getattr(getattr(product, "category", None), "name", "") or ""),
                unit_price=line.unit_price,
                quantity=line.quantity,
            )
            # Stock, exactly as SaleSerializer does it: the variant if there is
            # one, otherwise the product. F() so it is an atomic decrement.
            qty = line.quantity
            if variant is not None:
                ProductVariant.objects.unscoped().filter(pk=variant.pk).update(stock=F("stock") - qty)
            elif product is not None:
                Product.objects.unscoped().filter(pk=product.pk).update(stock=F("stock") - qty)

        sale.recalculate_total(save=False)
        spent = int(order.beans_spent or 0)
        sale.beans_spent = spent
        discounted = sale.total - points_service.value_of(spent)
        sale.discounted_total = discounted if discounted > 0 else Decimal("0.00")
        sale.save()

        order.sale = sale
        order.save(update_fields=["sale", "updated_at"])
        log.info("order %s collected -> sale %s (%s)", order.pk, sale.pk, sale.receipt_code)
        return sale

"""Recipes: what a drink is made of, and taking it out of stock.

The rules, stated once because people will check them against a till roll:

  * A drink's recipe is its RecipeLine rows with variant NULL. A size with
    any lines of its own uses ONLY those; a size with none uses the drink's.
  * A sale takes, for every line, recipe quantity × quantity sold — exact
    decimals in the item's base unit (piece / g / ml). One StockMove per
    (sale line, ingredient), so "where did the milk go" answers down to the
    receipt and the drink.
  * A voided sale is put back by a reversing move (never by deleting the
    original): the history keeps both. An edited sale reverses everything it
    took and takes again from its new lines.
  * A remake (a return with no refund) takes the recipe again — a second
    drink was made. A refund takes nothing back: the first drink was made.
  * Return-mode sales (is_return) touch no stock: a drink cannot go back on
    the shelf.
  * Stock may go negative. The till never blocks on inventory; the stock page
    flags the negative and a count corrects it.
  * The cost of a drink with a recipe is Σ quantity × the ingredient's unit
    cost (its last purchase price), frozen on the sale line when it is sold.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from apps.store import models

COST_PLACES = Decimal("0.0001")


def lines_for(store_id, product_id, variant_id=None) -> list:
    """The recipe that applies to this drink in this size."""
    qs = models.RecipeLine.objects.for_pharmacy(store_id).select_related("item")
    if variant_id:
        own = list(qs.filter(product_id=product_id, variant_id=variant_id))
        if own:
            return own
    return list(qs.filter(product_id=product_id, variant__isnull=True))


def cost_of(lines) -> Decimal | None:
    """Σ quantity × unit cost, to four places. None when there is no recipe."""
    if not lines:
        return None
    total = sum(
        (Decimal(l.quantity) * Decimal(l.item.unit_cost or 0) for l in lines), Decimal("0")
    )
    return total.quantize(COST_PLACES, rounding=ROUND_HALF_UP)


def _apply(store_id, plan, *, kind, sale=None, reason="", user=None):
    """Write the moves in `plan` = [(item_id, qty_signed, sale_item, product,
    product_name)] and move each item's stock. Items are locked in id order
    so two tills selling at once cannot deadlock or lose an update."""
    if not plan:
        return []
    ids = sorted({p[0] for p in plan})
    items = {
        i.pk: i
        for i in models.InventoryItem.objects.for_pharmacy(store_id)
        .select_for_update()
        .filter(pk__in=ids)
        .order_by("pk")
    }
    out = []
    for item_id, qty, sale_item, product_id, product_name in plan:
        item = items.get(item_id)
        if item is None or not qty:
            continue
        unit_cost = Decimal(item.unit_cost or 0)
        item.stock = Decimal(item.stock or 0) + qty
        out.append(
            models.StockMove(
                store_id=store_id,
                item=item,
                kind=kind,
                quantity=qty,
                unit_cost=unit_cost,
                total_cost=(abs(qty) * unit_cost).quantize(COST_PLACES, rounding=ROUND_HALF_UP),
                stock_after=item.stock,
                reason=reason,
                created_by=user if getattr(user, "is_authenticated", False) else None,
                sale=sale,
                sale_item_id=sale_item,
                product_id=product_id,
                product_name=product_name or "",
                receipt_code=getattr(sale, "receipt_code", "") or "",
            )
        )
    models.StockMove.objects.bulk_create(out)
    for item in items.values():
        item.save(update_fields=["stock", "updated_at"])
    return out


@transaction.atomic
def consume_sale(sale, *, user=None) -> int:
    """Take every line's recipe out of stock. Returns the moves written.

    Idempotent: a sale that already has its consumption (a retried sync, a
    second call) is left alone.
    """
    if sale is None or sale.is_return:
        return 0
    already = models.StockMove.objects.for_pharmacy(sale.store_id).filter(
        sale_id=sale.pk, kind=models.StockMove.Kind.SALE
    )
    if already.exists():
        net = already.aggregate(n=Sum("quantity"))["n"] or 0
        if net != 0:
            return 0
    plan = []
    for line in sale.items.all().order_by("pk"):
        if not line.product_id:
            continue
        name = " — ".join(x for x in [line.medication_name, line.variant_label] if x)
        for r in lines_for(sale.store_id, line.product_id, line.variant_id):
            plan.append((r.item_id, -(Decimal(r.quantity) * Decimal(line.quantity)), line.pk, line.product_id, name))
    return len(_apply(sale.store_id, plan, kind=models.StockMove.Kind.SALE, sale=sale, reason="بيع", user=user))


@transaction.atomic
def reverse_sale(sale, *, reason="إلغاء فاتورة", user=None) -> int:
    """Put back everything this sale still holds, line by line, ingredient by
    ingredient — so after it the sale's net SALE movement is exactly zero."""
    if sale is None:
        return 0
    rows = (
        models.StockMove.objects.for_pharmacy(sale.store_id)
        .filter(sale_id=sale.pk, kind=models.StockMove.Kind.SALE)
        .values("item_id", "sale_item_id", "product_id", "product_name")
        .annotate(net=Sum("quantity"))
    )
    plan = [
        (r["item_id"], -Decimal(r["net"]), r["sale_item_id"], r["product_id"], r["product_name"])
        for r in rows
        if r["net"]
    ]
    return len(_apply(sale.store_id, plan, kind=models.StockMove.Kind.SALE, sale=sale, reason=reason, user=user))


@transaction.atomic
def consume_remake(sale_return, *, user=None) -> int:
    """A drink made again: its recipe × the returned quantity."""
    line = sale_return.sale_item
    if line is None or not line.product_id:
        return 0
    sale = sale_return.sale
    name = " — ".join(x for x in [line.medication_name, line.variant_label] if x)
    plan = [
        (r.item_id, -(Decimal(r.quantity) * Decimal(sale_return.quantity)), line.pk, line.product_id, name)
        for r in lines_for(sale.store_id, line.product_id, line.variant_id)
    ]
    return len(
        _apply(sale.store_id, plan, kind=models.StockMove.Kind.REMAKE, sale=sale,
               reason="إعادة تحضير", user=user)
    )


def recipe_payload(store_id, product) -> dict:
    """Every version of one drink's recipe, for the drawer."""
    lines = list(
        models.RecipeLine.objects.for_pharmacy(store_id)
        .filter(product=product)
        .select_related("item")
        .order_by("position", "id")
    )
    by = defaultdict(list)
    for l in lines:
        by[l.variant_id].append(l)

    def ser(ls):
        return [
            {
                "id": l.pk,
                "item": l.item_id,
                "item_name": l.item.name,
                "base_unit": l.item.unit,
                "quantity": str(l.quantity),
                "display_unit": l.display_unit or l.item.unit,
                "unit_cost": str(l.item.unit_cost),
                "line_cost": str((Decimal(l.quantity) * Decimal(l.item.unit_cost or 0)).quantize(COST_PLACES)),
            }
            for l in ls
        ]

    variants = list(product.variants.all().order_by("id"))
    return {
        "product": product.pk,
        "base": ser(by.get(None, [])),
        "base_cost": str(cost_of(by.get(None, [])) or "") or None,
        "variants": [
            {
                "id": v.pk,
                "label": v.label,
                "own": bool(by.get(v.pk)),
                "lines": ser(by.get(v.pk, [])),
                "cost": str(cost_of(by.get(v.pk) or by.get(None, [])) or "") or None,
            }
            for v in variants
        ],
    }

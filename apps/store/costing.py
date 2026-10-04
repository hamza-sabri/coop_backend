"""Cost of a sold line, and whether selling moves menu stock.

Two small questions, asked from four places (counter sale, sale edit, app
order collection, void). Kept here so they can never answer differently.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings


def unit_cost_for(product, variant):
    """What one unit of this line costs the shop, or None if unknown.

    The cost the OWNER typed decides: the variant's own (a large latte costs
    more than a small one), else the drink's. Ingredients are for the shelf
    — what a sale takes from stock — not for pricing; their sum is used only
    when no cost was ever typed, so a drink with a recipe is never reported
    at 100% margin. Zero means "never entered", which is not the same as
    free — stored as NULL so the P&L can say how much revenue has no cost
    behind it.
    """
    for obj in (variant, product):
        if obj is None:
            continue
        cost = getattr(obj, "cost", None)
        if cost is not None and Decimal(cost) > 0:
            return Decimal(cost)
    if product is not None:
        from apps.store import recipes

        return recipes.cost_of(
            recipes.lines_for(product.store_id, product.pk, getattr(variant, "pk", None))
        )
    return None


def tracks_menu_stock() -> bool:
    """A café does not count lattes. Selling a drink leaves Product.stock
    alone unless TRACK_PRODUCT_STOCK is switched on (a retail vertical)."""
    return bool(getattr(settings, "TRACK_PRODUCT_STOCK", False))

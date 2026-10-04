"""Cost of a sold line, and whether selling moves menu stock.

Two small questions, asked from four places (counter sale, sale edit, app
order collection, void). Kept here so they can never answer differently.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings


def unit_cost_for(product, variant):
    """What one unit of this line costs the shop, or None if unknown.

    The variant's own cost wins (a large latte costs more than a small one);
    else the product's. Zero means "never entered", which is not the same as
    free — it is stored as NULL so the P&L can say how much revenue has no
    cost behind it instead of reporting a 100% margin.
    """
    for obj in (variant, product):
        if obj is None:
            continue
        cost = getattr(obj, "cost", None)
        if cost is not None and Decimal(cost) > 0:
            return Decimal(cost)
    return None


def tracks_menu_stock() -> bool:
    """A café does not count lattes. Selling a drink leaves Product.stock
    alone unless TRACK_PRODUCT_STOCK is switched on (a retail vertical)."""
    return bool(getattr(settings, "TRACK_PRODUCT_STOCK", False))

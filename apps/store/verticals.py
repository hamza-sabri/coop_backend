"""What kind of shop is this deployment for?

The whole system is generic retail: tenants, POS, inventory, customers, a
credit ledger, purchase orders, offline, reports. A pharmacy and a supermarket
differ in vocabulary and in which optional fields matter — not in mechanics.

So verticals are CONFIGURATION, never forks. Forking means fixing every bug N
times; this way a fix lands once for everyone.

Set VERTICAL in the environment. Add a new one by adding a dict here.
"""
import os

VERTICALS = {
    "pharmacy": {
        "label_ar": "صيدلية",
        "product_label_ar": "دواء",
        # Pharmacies live and die by expiry dates.
        "track_expiry": True,
        "default_modules": [],  # empty = every module
    },
    "supermarket": {
        "label_ar": "سوبرماركت",
        "product_label_ar": "منتج",
        # Groceries expire too — on by default, unlike a hardware shop.
        "track_expiry": True,
        "default_modules": [],
    },
    "cafe": {
        "label_ar": "كوفي شوب",
        "product_label_ar": "صنف",
        # Drinks are made to order; nothing sits on a shelf long enough to expire.
        "track_expiry": False,
        "default_modules": [],
    },
    "shop": {
        "label_ar": "متجر",
        "product_label_ar": "منتج",
        "track_expiry": False,
        "default_modules": [],
    },
}

VERTICAL = os.getenv("VERTICAL", "shop")


def config() -> dict:
    return VERTICALS.get(VERTICAL, VERTICALS["shop"])

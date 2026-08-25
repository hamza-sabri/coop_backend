"""Feature modules — the units a store subscribes to.

A tenant doesn't have to buy the whole system: one store may want only the
POS, another only the customer price-check, another just the debt ledger.
Access is controlled at TWO levels:

  1. Store level — `Store.enabled_modules` (what the tenant pays for).
  2. User level — `User.allowed_modules` (what the owner lets each staff
     account touch, e.g. a cashier gets POS only).

An EMPTY list means "no restriction" at that level, so existing tenants and
accounts keep full access with no backfill. A user's effective access is the
intersection of both levels.

If a module key is retired, stale entries in stored lists are ignored — never
rename a key in place.
"""

from __future__ import annotations

# key -> Arabic label (shown in admin / tiers page copy)
MODULES: dict[str, str] = {
    "inventory": "المخزون والأدوية",
    "pos": "نقطة البيع",
    "customers": "الزبائن",
    "debts": "الديون والدفاتر",
    "price_check": "استعلام الأسعار للزبائن",
    "imports": "الاستيراد من حسابات",
    "reports": "التقارير والتحليلات",
    "sales_reports": "تقارير المبيعات",
    "scan_reports": "تقارير مسح الأسعار",
    "purchases": "المشتريات وطلبات الشراء",
    "offline": "العمل بدون إنترنت",
    # Granular offline capabilities (a tenant can buy offline per area). Having
    # "offline" (the umbrella) implies all of these; they can also be sold à la
    # carte. The frontend enables the local mirror + write queue per key.
    "offline_pos": "نقطة البيع بدون إنترنت",
    "offline_debts": "الديون بدون إنترنت",
    "offline_inventory": "المخزون بدون إنترنت",
    "offline_customers": "الزبائن بدون إنترنت",
    "offline_purchases": "المشتريات بدون إنترنت",
}

ALL_MODULES = frozenset(MODULES)


def normalize(mods) -> list[str]:
    """Keep only known module keys, deduped, stable order."""
    seen: list[str] = []
    for m in mods or []:
        if m in MODULES and m not in seen:
            seen.append(m)
    return seen


def pharmacy_modules(store) -> frozenset:
    """Modules the tenant has.

    With a plan assigned: the plan's modules ∪ enabled_modules (à-la-carte
    extras) — explicit, no "empty = everything" magic. Without a plan the
    legacy rule holds: empty stored list = everything.
    """
    if store is None:
        return frozenset()
    enabled = normalize(getattr(store, "enabled_modules", None))
    plan = getattr(store, "plan", None)
    if plan is not None and getattr(plan, "is_active", True):
        return frozenset(normalize(plan.modules)) | frozenset(enabled)
    return frozenset(enabled) if enabled else ALL_MODULES


def effective_modules(user) -> frozenset:
    """What THIS staff account may use = store modules ∩ user modules.

    Either list being empty means "unrestricted at that level" — the
    intersection still applies the other level.
    """
    if user is None or not getattr(user, "store_id", None):
        return frozenset()
    tenant = pharmacy_modules(user.store)
    allowed = normalize(getattr(user, "allowed_modules", None))
    return tenant & frozenset(allowed) if allowed else tenant

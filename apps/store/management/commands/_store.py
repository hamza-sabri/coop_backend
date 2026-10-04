"""Which shop a showcase command acts on.

Order: --store if given, else the deployment's own shop (CLERK_STORE_SLUG —
the slug the customer app is pinned to, `coop` in production), else the only
shop in the database. Never a guess between several: that is an error that
lists them.
"""
from django.conf import settings
from django.core.management.base import CommandError

from apps.store.models import Store


def resolve_store(slug: str | None) -> Store:
    if slug:
        store = Store.objects.filter(slug=slug).first()
        if store is None:
            raise CommandError(f"no store with slug {slug!r} — stores here: {_slugs()}")
        return store
    configured = (getattr(settings, "CLERK_STORE_SLUG", "") or "").strip()
    if configured:
        store = Store.objects.filter(slug=configured).first()
        if store is not None:
            return store
    stores = list(Store.objects.all()[:2])
    if len(stores) == 1:
        return stores[0]
    raise CommandError(f"which store? pass --store <slug> — stores here: {_slugs()}")


def _slugs() -> str:
    return ", ".join(Store.objects.values_list("slug", flat=True)) or "(none)"

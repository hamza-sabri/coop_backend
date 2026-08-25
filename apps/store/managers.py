"""Mechanical tenant scoping — HARD ISOLATION RULE №2 (plan §3.5).

Every tenant-owned model's default manager is a `TenantManager`: a direct
read (`Model.objects.all()/filter()/get()/…`) RAISES `TenantScopeError`
instead of silently returning cross-store rows. Application code must go
through one of two explicit, greppable doors:

    Model.objects.for_pharmacy(pharmacy_or_id)   # the normal path
    Model.objects.unscoped()                     # admin/ops/tests ONLY

`unscoped()` usages are code-reviewed: they may appear in the Django admin,
management commands, migrations, platform-wide (non-identifying) aggregates,
and test setup — never in a tenant/public request path.

What stays allowed without a scope (deliberately):
- Django-generated RELATED managers (`store.products`, `med.images`,
  `sale.items`, …): they subclass this manager but are already filtered
  through their parent instance.
- WRITES (`create/get_or_create/update_or_create/bulk_create/bulk_update`):
  every write in this codebase carries an explicit store (serializers set
  it server-side; the DB's NOT NULL FK is the backstop).
- Django internals (forward FK access, cascade deletion, refresh_from_db):
  they use the model's base manager, which each tenant model points at a
  plain `models.Manager` via `Meta.base_manager_name = "unguarded"`. That
  manager is an implementation detail — application code never touches it.
"""
from django.db import models


class TenantScopeError(Exception):
    """An unscoped query hit a tenant-owned model. Scope it or say why not."""


class TenantManager(models.Manager):
    """Default manager for tenant-owned models. See module docstring.

    `tenant_field` is the filter path from the model to its store id
    (e.g. "store_id", "medication__pharmacy_id", "sale__pharmacy_id").
    """

    def __init__(self, tenant_field="store_id"):
        super().__init__()
        self.tenant_field = tenant_field

    def get_queryset(self):
        # Django builds related managers as SUBCLASSES of the default manager
        # (med.images, store.products, sale.items…). Those are already
        # scoped through the parent instance — let them through. Only a
        # direct `Model.objects.<read>` is an unscoped query.
        if type(self) is not TenantManager:
            return super().get_queryset()
        raise TenantScopeError(
            f"Unscoped query on {self.model.__name__}. Use "
            f"{self.model.__name__}.objects.for_pharmacy(store) — or "
            f".unscoped() ONLY in admin/ops/test code (code-reviewed)."
        )

    def for_pharmacy(self, store):
        """All rows of ONE store. Accepts a Store instance or its id."""
        pid = getattr(store, "pk", store)
        if not pid:
            raise TenantScopeError(
                f"for_pharmacy() on {self.model.__name__} needs a store "
                f"or a non-empty store id (got {store!r})."
            )
        return super().get_queryset().filter(**{self.tenant_field: pid})

    def unscoped(self):
        """The explicit cross-tenant escape hatch — admin/ops/tests only."""
        return super().get_queryset()

    # ── Writes stay open (they always carry an explicit store) ───────────
    def create(self, **kwargs):
        return self.unscoped().create(**kwargs)

    def get_or_create(self, defaults=None, **kwargs):
        return self.unscoped().get_or_create(defaults=defaults, **kwargs)

    def update_or_create(self, defaults=None, **kwargs):
        return self.unscoped().update_or_create(defaults=defaults, **kwargs)

    def bulk_create(self, objs, *args, **kwargs):
        return self.unscoped().bulk_create(objs, *args, **kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        return self.unscoped().bulk_update(objs, fields, **kwargs)

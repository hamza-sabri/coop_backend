from rest_framework.exceptions import APIException
from rest_framework.permissions import SAFE_METHODS, BasePermission


class StoreRequired(APIException):
    """A tenant endpoint was called with no resolvable store → HTTP 400.

    The client contract is exact and machine-checkable:
        {"detail": "store_id is required"}
    """

    status_code = 400
    default_detail = "store_id is required"
    default_code = "pharmacy_required"


class StoreResolved(BasePermission):
    """THE tenant-API 400 guard — the single choke point (plan §3.5 rule 3).

    Every endpoint on the tenant API surface declares this permission (the
    viewsets inherit it through `StoreScopedMixin`, the APIViews list it
    explicitly). A request that cannot resolve a store is rejected up
    front with 400 {"detail": "store_id is required"} — before any view
    logic, queryset, or serializer runs.

    Resolution sources, by view kind:
    - Anonymous/public tenant views set `pharmacy_slug_param` (e.g.
      "store"): the store comes ONLY from that query param. Present and
      non-empty → resolved (an unknown slug is then the view's own concern:
      it answers not-found/empty, never another tenant's data).
    - Staff views (no `pharmacy_slug_param`): the store comes ONLY from
      the authenticated user. `request_pharmacy_id()` raises the same 400 as
      the backstop for anything this permission does not cover.

    DELIBERATELY EXEMPT (no store by design — keep this list in sync with
    the tests in apps/store/tests/test_pharmacy_required.py):
    - /api/v1/auth/*             login/refresh/logout/me
    - /healthz/                  DB-free liveness probe
    - /admin/*                   Django admin (cross-tenant ops)
    - /api/schema/, /api/docs/*  OpenAPI + docs; static files
    - /api/v1/public/stats/      platform-wide marketing aggregates
    - /api/v1/pos/cart-state/    per-USER resource (scoped by account, not tenant)
    """

    def has_permission(self, request, view):
        slug_param = getattr(view, "pharmacy_slug_param", None)
        if slug_param is not None:
            if (request.query_params.get(slug_param) or "").strip():
                return True
            raise StoreRequired()
        user = getattr(request, "user", None)
        if user and user.is_authenticated and getattr(user, "store_id", None):
            return True
        raise StoreRequired()


class IsAdminOrReadOnly(BasePermission):
    """Anyone can read; only staff can write."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_staff)


class IsOwnerOrReadOnly(BasePermission):
    """Object-level: safe methods for all, writes only for the owner.

    Set `owner_field` on the view (default "user") to point at the FK that
    identifies ownership.
    """

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        owner_field = getattr(view, "owner_field", "user")
        return getattr(obj, owner_field, None) == request.user


class ModuleEnabled(BasePermission):
    """Feature-module gate — the SaaS tier enforcement point.

    A view declares `required_module = "pos"` (or a tuple meaning ANY-of,
    e.g. `("customers", "debts", "pos")`). Access passes only when that
    module is in the user's effective set: the intersection of what the
    store subscribes to (`Store.enabled_modules`) and what the staff
    account is allowed (`User.allowed_modules`) — empty list = unrestricted
    at that level.

    Views with no `required_module` are unaffected. Accounts with no
    store are left to `StoreResolved` to reject (400
    "store_id is required") so the error message stays consistent.
    """

    message = "هذه الخاصية غير مفعّلة لحسابكم."

    def has_permission(self, request, view):
        required = getattr(view, "required_module", None)
        if not required:
            return True
        if isinstance(required, str):
            required = (required,)
        user = request.user
        if not user or not user.is_authenticated or not getattr(user, "store_id", None):
            return True  # tenant scoping rejects these with its own 403/401
        from apps.store.modules import effective_modules

        mods = effective_modules(user)
        return any(m in mods for m in required)


class OwnerRequired(BasePermission):
    """Role gate — store OWNERS only (or platform superusers).

    Employees are day-to-day staff: POS, sales, inventory, customers, debts.
    Sensitive tenant-wide features (reports/analytics, Hesabate imports, bulk
    deletes, the price-page QR) declare this permission so an employee account
    can never reach them, regardless of module grants.
    """

    message = "هذه الميزة متاحة لمالك الصيدلية فقط. تواصل مع الإدارة لتفعيلها."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (user.is_superuser or getattr(user, "role", "") == "owner")
        )


class IsSelfOrAdmin(BasePermission):
    """Object-level: a user may act on their own record; staff on any."""

    def has_object_permission(self, request, view, obj):
        if request.user and request.user.is_staff:
            return True
        return obj == request.user

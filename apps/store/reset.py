"""Wiping a shop back to its menu — the "start fresh" button.

Written for one real situation: the pilot is over, the shop wants to open for
real, and the database is full of test orders, test customers and test points
that would otherwise be somebody's opening balance.

Three things about this module are deliberate.

1.  It is TENANT-SCOPED, always, with no option not to be. Every query filters
    on one store id. A reset that could reach another shop's sales is not a
    feature, it is an incident waiting for a mis-click.

2.  It KEEPS the menu, the staff and the shop itself. "Everything except the
    menu" is how the request is phrased, but staff accounts are not what
    anybody means by that — deleting them locks the owner out of the admin he
    pressed the button in. Kept, and said out loud on the confirmation page.

3.  Firebase is deleted LAST, after the database transaction has committed.
    The two systems cannot be wiped atomically: if the transaction rolled back
    after the auth users were gone, customers would be locked out of accounts
    the database still believed in. Doing the reversible half first means the
    worst case is orphaned Firebase users, who can simply sign in again — and
    signing in again is the whole point of deleting them.

`preview()` counts without touching anything, and the admin page shows that
before it will accept a confirmation.
"""
from __future__ import annotations

import logging

from django.db import transaction

log = logging.getLogger(__name__)

#: Deleted, in dependency order — children before parents so a FK never blocks.
#: The tuples are (label, model attribute, tenant filter kwargs builder).
WIPES: list[tuple[str, str, str]] = [
    ("بنود الفواتير", "SaleItem", "sale__store_id"),
    ("مراجعات الفواتير", "SaleRevision", "sale__store_id"),
    ("الفواتير", "Sale", "store_id"),
    ("بنود الطلبات", "OrderItem", "order__store_id"),
    ("الطلبات", "Order", "store_id"),
    ("بنود الديون", "DebtItem", "debt__store_id"),
    ("الديون", "Debt", "store_id"),
    ("بنود المشتريات", "PurchaseItem", "order__store_id"),
    ("أوامر الشراء", "PurchaseOrder", "store_id"),
    ("سجل النقاط", "BeanLedger", "store_id"),
    ("ملفات الولاء", "LoyaltyProfile", "store_id"),
    ("الإشعارات", "Notification", "store_id"),
    ("اشتراكات الويب بوش", "PushSubscription", "store_id"),
    ("أجهزة التطبيق", "DeviceToken", "store_id"),
    ("سجل الاستعلامات", "ScanDaily", "store_id"),
    ("سجل التغييرات", "AuditLog", "store_id"),
    # Customers LAST of the rows: everything above points at them.
    ("الزبائن", "Customer", "store_id"),
]

#: Never touched. Listed so the confirmation page can say so.
KEPT = [
    "المنيو (الأصناف والأحجام والصور والتصنيفات)",
    "حسابات الموظفين والمالك",
    "إعدادات المتجر والخطة",
    "سلال نقاط البيع المفتوحة (مرتبطة بالموظف، لا بالمتجر)",
]


def _model(name: str):
    from apps.store import models as m

    return getattr(m, name)


def preview(store) -> list[tuple[str, int]]:
    """What a reset would delete, counted. Touches nothing."""
    out: list[tuple[str, int]] = []
    for label, name, field in WIPES:
        model = _model(name)
        n = model.unguarded.filter(**{field: store.pk}).count()
        out.append((label, n))
    return out


def firebase_uids(store) -> list[str]:
    """The Firebase accounts belonging to THIS shop's customers."""
    from apps.store.models import Customer

    return list(
        Customer.unguarded.filter(store_id=store.pk)
        .exclude(firebase_uid="")
        .values_list("firebase_uid", flat=True)
    )


@transaction.atomic
def wipe_database(store) -> list[tuple[str, int]]:
    """Delete the shop's history. Returns what was deleted, in order.

    One transaction: a reset that half-happens leaves orders pointing at
    customers who no longer exist, which is worse than either outcome.
    """
    done: list[tuple[str, int]] = []
    for label, name, field in WIPES:
        model = _model(name)
        n, _ = model.unguarded.filter(**{field: store.pk}).delete()
        done.append((label, n))
        log.warning("reset: store=%s %s -> %s rows", store.slug, name, n)
    return done


# ---------------------------------------------------------------------------
# Firebase Authentication
# ---------------------------------------------------------------------------
#
# The Identity Toolkit REST API, not firebase-admin: this project already has
# google-auth for FCM and adding the whole Admin SDK to delete some accounts
# would be a large dependency for one call. Same service-account JSON, a
# different OAuth scope.

IDENTITY_SCOPE = "https://www.googleapis.com/auth/identitytoolkit"
BATCH_DELETE = "https://identitytoolkit.googleapis.com/v1/projects/{pid}/accounts:batchDelete"

#: Google's cap per request.
BATCH = 1000


def _identity_token() -> str | None:
    from apps.store import push as push_service

    # Same JSON as FCM, a different scope — and the same forgiving reader, so
    # a base64-pasted or newline-mangled variable works here too.
    info = push_service.credentials_info()
    if info is None:
        return None
    try:
        import google.auth.transport.requests
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[IDENTITY_SCOPE]
        )
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token
    except Exception as exc:  # noqa: BLE001
        log.error("reset: could not mint an identitytoolkit token: %s", exc)
        return None


def delete_firebase_users(uids: list[str]) -> tuple[int, str]:
    """Delete these Firebase accounts. Returns (deleted, message).

    `force` is required: without it Google refuses to delete accounts that are
    not already disabled, which is every account here.

    Failure is reported, never raised. The database half has already committed
    by the time this runs, and an auth account that outlives its customer row
    is harmless — the next sign-in simply creates a fresh customer, which is
    exactly what the reset was for.
    """
    if not uids:
        return 0, "لا يوجد حسابات مرتبطة بفايربيس"

    from django.conf import settings

    pid = (getattr(settings, "FIREBASE_PROJECT_ID", "") or "").strip()
    if not pid:
        return 0, "FIREBASE_PROJECT_ID غير مضبوط — لم تُحذف حسابات فايربيس"
    token = _identity_token()
    if token is None:
        return 0, "FIREBASE_CREDENTIALS غير مضبوط أو غير صالح — لم تُحذف حسابات فايربيس"

    import requests

    url = BATCH_DELETE.format(pid=pid)
    deleted = 0
    for i in range(0, len(uids), BATCH):
        chunk = uids[i : i + BATCH]
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"localIds": chunk, "force": True},
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            return deleted, f"تعذّر الاتصال بفايربيس: {exc}"
        if r.status_code != 200:
            return deleted, f"فايربيس رفض الحذف ({r.status_code}): {r.text[:200]}"
        # Per-account failures come back in the body, not the status code.
        errors = (r.json() or {}).get("errors") or []
        deleted += len(chunk) - len(errors)
        if errors:
            log.warning("reset: firebase refused %s accounts: %s", len(errors), errors[:3])
    return deleted, f"حُذف {deleted} حساب من فايربيس"

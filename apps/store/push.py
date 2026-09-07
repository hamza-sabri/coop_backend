"""Notifications: the record first, the push second.

The ordering in `notify()` is the whole design. A `Notification` row is written
and committed before any network call is attempted, because push fails
constantly and in ways nobody can see — permission never granted, the phone in
a tunnel, a token that rotated an hour ago, FCM having a bad afternoon. A
customer who opens the app and finds "طلبك جاهز" waiting for them has been told.
A push that evaporated has not, and there would be no trace it ever existed.

So push is a delivery ATTEMPT on top of a record that already exists, never the
record itself. `pushed_at` says an attempt was handed to Google; it says nothing
about whether a human ever saw it.

Sending needs a service account (FIREBASE_CREDENTIALS). Verifying sign-ins does
not — see accounts/firebase. If the credentials are absent everything here
degrades to writing rows and returning quietly, which is exactly what should
happen in local development and in a deployment where push is not set up yet.
"""
from __future__ import annotations

import json
import logging
import threading

from django.conf import settings
from django.db import transaction
from django.utils import timezone

log = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{pid}/messages:send"

_creds_lock = threading.Lock()
_creds = None


def credentials_info() -> dict | None:
    """The service-account JSON as a dict, however it was pasted in.

    FIREBASE_CREDENTIALS holds the JSON itself rather than a path, because the
    deployment is a container and a secret that has to exist as a file on disk
    is a secret that ends up baked into an image. That is the right trade, but
    it puts a 1,700-character value with embedded `\n` escapes through a web
    form, and the two ways that goes wrong are both silent:

    *   the panel (or a shell, or a copy-paste through an editor) converts the
        `\n` escapes in `private_key` into REAL line breaks, which is no longer
        valid JSON — and often truncates the value at the first one;
    *   nothing is pasted at all and the variable stays empty.

    So three shapes are accepted. Base64 is the one to prefer, because it has
    no newlines, no quotes and no backslashes and therefore nothing left for a
    form to mangle: `base64 -w0 service-account.json`.
    """
    raw = (getattr(settings, "FIREBASE_CREDENTIALS", "") or "").strip()
    if not raw:
        return None

    # 1. base64 of the JSON — the shape that survives any input box.
    if not raw.startswith("{"):
        import base64

        try:
            decoded = base64.b64decode(raw, validate=True).decode("utf-8").strip()
            if decoded.startswith("{"):
                raw = decoded
        except Exception:  # noqa: BLE001
            pass

    # 2. the JSON as-is.
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        pass

    # 3. the JSON with the PEM's newline escapes eaten. Repaired rather than
    #    rejected: the value is still complete, it is only mis-quoted, and
    #    telling an owner to re-paste a key he already pasted correctly-looking
    #    is not a fix he can act on.
    import re

    m = re.search(r'("private_key"\s*:\s*")(.*?)("\s*[,}])', raw, re.S)
    if m:
        body = m.group(2).replace("\r", "").replace("\n", "\\n")
        try:
            return json.loads(raw[: m.start(2)] + body + raw[m.end(2) :])
        except Exception:  # noqa: BLE001
            pass
    return None


def _credentials():
    """Service-account credentials, parsed once and refreshed in place."""
    global _creds
    info = credentials_info()
    if info is None:
        return None
    with _creds_lock:
        if _creds is None:
            try:
                from google.oauth2 import service_account

                _creds = service_account.Credentials.from_service_account_info(
                    info, scopes=[FCM_SCOPE]
                )
            except Exception as exc:  # noqa: BLE001
                log.error("push: bad FIREBASE_CREDENTIALS: %s", exc)
                return None
        return _creds


def _access_token() -> str | None:
    creds = _credentials()
    if creds is None:
        return None
    try:
        import google.auth.transport.requests

        if not creds.valid:
            creds.refresh(google.auth.transport.requests.Request())
        return creds.token
    except Exception as exc:  # noqa: BLE001
        log.error("push: could not mint an access token: %s", exc)
        return None


def _send_one(token: str, title: str, body: str, data: dict) -> str:
    """Deliver to one device. Returns 'ok', 'gone', or 'error'.

    'gone' is the important one: FCM reports an unregistered or invalid token
    and the row must be deleted. Left in place, every future notification fans
    out to a device that no longer exists, and the failure rate creeps up until
    it looks like push itself is broken.
    """
    import requests

    access = _access_token()
    pid = (getattr(settings, "FIREBASE_PROJECT_ID", "") or "").strip()
    if not access or not pid:
        return "error"

    # Every value in an FCM data payload must be a string. A dict with an int
    # in it is rejected wholesale with a 400 that does not say which key.
    flat = {str(k): ("" if v is None else str(v)) for k, v in (data or {}).items()}

    message = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body or ""},
            "data": flat,
            "android": {
                "priority": "high",
                "notification": {
                    "channel_id": "koup_orders",
                    "sound": "default",
                },
            },
            "apns": {
                "headers": {"apns-priority": "10"},
                "payload": {"aps": {"sound": "default", "badge": 1}},
            },
        }
    }

    try:
        resp = requests.post(
            FCM_ENDPOINT.format(pid=pid),
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=message,
            timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("push: transport failed: %s", exc)
        return "error"

    if resp.status_code == 200:
        return "ok"
    if resp.status_code in (400, 403, 404):
        text = resp.text or ""
        if "UNREGISTERED" in text or "NOT_FOUND" in text or "INVALID_ARGUMENT" in text:
            return "gone"
    log.warning("push: fcm %s %s", resp.status_code, (resp.text or "")[:300])
    return "error"


def push_to_customer(store, customer, title: str, body: str, data: dict) -> int:
    """Fan out to every device this customer has. Returns how many succeeded."""
    from apps.store.models import DeviceToken

    if not (getattr(settings, "FIREBASE_CREDENTIALS", "") or ""):
        return 0

    tokens = list(
        DeviceToken.objects.for_pharmacy(store.pk)
        .filter(customer=customer)
        .values_list("id", "token")
    )
    if not tokens:
        return 0

    sent, dead = 0, []
    for row_id, token in tokens:
        outcome = _send_one(token, title, body, data)
        if outcome == "ok":
            sent += 1
        elif outcome == "gone":
            dead.append(row_id)

    if dead:
        DeviceToken.objects.unscoped().filter(id__in=dead).delete()
        log.info("push: pruned %d dead token(s)", len(dead))
    return sent


def notify(store, customer, kind: str, title: str, body: str = "", data: dict | None = None):
    """Record it, then try to push it. Never raises into the caller.

    Called from inside order transactions, so the push itself is deferred with
    `transaction.on_commit`: a notification must not go out for an order that
    then rolls back, and an FCM timeout must not hold a database transaction
    open while a barista waits for the screen to respond.
    """
    from apps.store.models import Notification

    if customer is None:
        return None

    note = Notification.objects.create(
        store=store,
        customer=customer,
        kind=kind,
        title=title,
        body=body or "",
        data=data or {},
    )

    def _deliver():
        try:
            sent = push_to_customer(store, customer, title, body, {
                **(data or {}), "kind": kind, "notification_id": note.pk,
            })
            if sent:
                Notification.objects.unscoped().filter(pk=note.pk).update(
                    pushed_at=timezone.now()
                )
        except Exception as exc:  # noqa: BLE001
            # A failed push is not a failed order. Log and move on.
            log.warning("push: delivery failed for notification %s: %s", note.pk, exc)

    transaction.on_commit(_deliver)
    return note


# ---------------------------------------------------------------------------
# The specific things worth telling someone
# ---------------------------------------------------------------------------

#: Status → (kind, title, body). Kept as data rather than a chain of ifs so the
#: app, the admin and the push text can never drift out of agreement.
ORDER_MESSAGES = {
    "placed": ("order_placed", "تم إرسال طلبك", "بانتظار تأكيد المقهى"),
    "accepted": ("order_accepted", "تم قبول طلبك", "سنبدأ بتحضيره حالاً"),
    "preparing": ("order_preparing", "طلبك قيد التحضير", "لحظات ويصبح جاهزاً"),
    "ready": ("order_ready", "طلبك جاهز", "تفضّل لاستلامه من الكاونتر"),
    "collected": ("order_collected", "تم استلام طلبك", "بالهنا والشفا"),
    "cancelled": ("order_cancelled", "أُلغي طلبك", ""),
}


def notify_order_status(order):
    """Tell the customer their order moved. Safe to call on every transition."""
    entry = ORDER_MESSAGES.get(order.status)
    if not entry:
        return None
    kind, title, body = entry
    if order.status == "cancelled" and order.cancelled_reason:
        body = order.cancelled_reason
    number = getattr(order, "number", None) or order.pk
    return notify(
        order.store, order.customer, kind, title, body,
        {"order_id": order.pk, "order_number": number, "status": order.status},
    )


def notify_points(store, customer, delta: int, balance: int, reason: str = ""):
    """Beans moved. Earning and spending read differently, so they are told
    differently — 'you gained' and 'you spent' are not the same news."""
    if not delta:
        return None
    if delta > 0:
        kind, title = "points_earned", f"+{delta} حبة"
        body = reason or f"رصيدك الآن {balance} حبة"
    else:
        kind, title = "points_spent", f"{abs(delta)} حبة مستبدلة"
        body = reason or f"بقي لديك {balance} حبة"
    return notify(store, customer, kind, title, body,
                  {"delta": delta, "balance": balance})

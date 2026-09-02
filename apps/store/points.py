"""Loyalty points: the ONE place the maths lives.

The scheme, in a sentence: a customer earns 2% of what they spend back as
points, and 10 points is worth 1 shekel. So points are just shekels at ten
times the denomination, and a 20 ₪ order returns 4 points — 0.40 ₪ of value.

Everything here exists because the arithmetic was previously scattered: the
browser divided by 3.33, one view multiplied by it, and a third constant said
260 points bought a free cup. Three places to change, and a customer at the
counter who could be told a different number from the one on their phone.

Two invariants this module enforces, and neither is optional:

  1.  The LEDGER is the truth. `LoyaltyProfile.beans` is a cache of the ledger's
      running sum, kept only so a balance read is one row instead of an
      aggregate. Every movement writes a ledger row inside the same
      transaction, so the two can never diverge without a crash between them.

  2.  Every movement carries an idempotency key. A till that retries on a bad
      connection, a webhook delivered twice, a barista double-tapping
      "collected" — all of these must move a balance exactly once. The key is
      derived from what caused the movement, never from a timestamp.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction

log = logging.getLogger(__name__)

#: Fraction of the amount paid that comes back as value. 0.02 = 2%.
EARN_RATE = Decimal(str(getattr(settings, "POINTS_EARN_RATE", "0.02")))

#: How many points make one shekel. 10 means a point is worth 10 agorot.
POINTS_PER_ILS = int(getattr(settings, "POINTS_PER_ILS", 10))


def points_for(amount) -> int:
    """Points earned on `amount` shekels spent.

    Rounded half-up to a whole point: a customer who spends 17.50 ₪ earns 4
    points, not 3.5. Fractional points would have to be displayed, explained
    and stored, and they buy nothing.
    """
    if amount is None:
        return 0
    amt = Decimal(str(amount))
    if amt <= 0:
        return 0
    raw = amt * EARN_RATE * POINTS_PER_ILS
    return int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def value_of(points: int) -> Decimal:
    """What `points` are worth, in shekels."""
    if not points or points <= 0:
        return Decimal("0.00")
    return (Decimal(int(points)) / Decimal(POINTS_PER_ILS)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def max_spendable(balance: int, amount) -> int:
    """Most points that may be applied to a bill of `amount`.

    Bounded by BOTH the balance and the bill: points cannot buy more than the
    drink costs, and a redemption must never take a total below zero. Floor,
    not round — rounding up here would hand out value that was not earned.
    """
    if not balance or balance <= 0 or amount is None:
        return 0
    amt = Decimal(str(amount))
    if amt <= 0:
        return 0
    by_bill = int((amt * Decimal(POINTS_PER_ILS)).to_integral_value(rounding="ROUND_FLOOR"))
    return max(0, min(int(balance), by_bill))


def balance_of(customer) -> int:
    profile = getattr(customer, "loyalty", None)
    return int(getattr(profile, "beans", 0) or 0)


@transaction.atomic
def move(store, customer, delta: int, reason: str, note: str, idempotency_key: str):
    """Apply a signed movement. Returns (ledger_row, applied: bool).

    `applied` is False when the key has been seen before — the caller's retry
    was already honoured, and the balance must not move again. Callers should
    treat that as success, because it is.
    """
    from apps.store.models import BeanLedger, LoyaltyProfile

    if customer is None or not delta:
        return None, False

    delta = int(delta)

    # Lock the profile row, not the customer: two tills ringing up the same
    # regular at once would otherwise both read the same balance and both write
    # it, losing one of the movements entirely.
    profile, _ = LoyaltyProfile.objects.select_for_update().get_or_create(
        store=store, customer=customer, defaults={"beans": 0}
    )
    have = int(profile.beans or 0)

    # A redemption can never take the balance negative, whatever the caller
    # believed the balance was when it built the request.
    if delta < 0 and have + delta < 0:
        delta = -have
        if delta == 0:
            return None, False

    row, created = BeanLedger.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "store": store,
            "customer": customer,
            "delta": delta,
            "reason": reason,
            "balance_after": have + delta,
            "note": note or "",
        },
    )
    if not created:
        return row, False

    profile.beans = have + delta
    profile.save(update_fields=["beans"])
    return row, True


def award_for_purchase(store, customer, amount, *, source: str, source_id) -> int:
    """Mint points for a completed purchase. Returns the points awarded.

    Called when the goods are actually handed over — an order marked collected,
    or a sale rung up — never when an order is merely placed. An order that is
    cancelled must not have already paid out.
    """
    if customer is None:
        return 0
    points = points_for(amount)
    if points <= 0:
        return 0

    from apps.store.models import BeanLedger

    _, applied = move(
        store, customer, points, BeanLedger.Reason.EARN,
        f"نقاط {source} #{source_id}",
        f"earn:{source}:{source_id}",
    )
    return points if applied else 0


def spend_on_purchase(store, customer, points: int, amount, *, source: str, source_id) -> int:
    """Redeem points against a bill. Returns how many were actually spent.

    Clamped server-side against the live balance and the bill, because the
    number the phone or the till sent was true thirty seconds ago and may not
    be now.
    """
    if customer is None or not points or points <= 0:
        return 0

    from apps.store.models import BeanLedger

    spend = max_spendable(balance_of(customer), amount)
    spend = min(int(points), spend)
    if spend <= 0:
        return 0

    _, applied = move(
        store, customer, -spend, BeanLedger.Reason.REDEEM,
        f"استبدال في {source} #{source_id}",
        f"redeem:{source}:{source_id}",
    )
    return spend if applied else 0


def refund_spend(store, customer, points: int, *, source: str, source_id) -> int:
    """Give back points spent on something that did not happen."""
    if customer is None or not points or points <= 0:
        return 0
    from apps.store.models import BeanLedger

    _, applied = move(
        store, customer, int(points), BeanLedger.Reason.ADJUST,
        f"إلغاء {source} #{source_id}",
        f"refund:{source}:{source_id}",
    )
    return int(points) if applied else 0


def reverse_award(store, customer, amount, *, source: str, source_id) -> int:
    """Take back points minted for a purchase that was then returned.

    Deliberately allowed to be clipped by `move()` if the customer has already
    spent them: the shop eats the difference rather than putting someone into a
    negative balance they cannot understand or clear.
    """
    if customer is None:
        return 0
    points = points_for(amount)
    if points <= 0:
        return 0
    from apps.store.models import BeanLedger

    _, applied = move(
        store, customer, -points, BeanLedger.Reason.ADJUST,
        f"إرجاع {source} #{source_id}",
        f"reverse:{source}:{source_id}",
    )
    return points if applied else 0

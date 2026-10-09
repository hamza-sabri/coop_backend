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
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction

log = logging.getLogger(__name__)

#: Fraction of the amount paid that comes back as value. 0.02 = 2%.
EARN_RATE = Decimal(str(getattr(settings, "POINTS_EARN_RATE", "0.02")))

#: The DEFAULT number of points that make one shekel (10 = a point is worth
#: 10 agorot). Each store may set its own (Store.points_per_ils); always ask
#: per_ils(store), never read this directly for money.
POINTS_PER_ILS = int(getattr(settings, "POINTS_PER_ILS", 10))


def per_ils(store=None) -> int:
    """How many points make one shekel in this store."""
    if store is None:
        return POINTS_PER_ILS
    v = getattr(store, "points_per_ils", "missing")
    if v == "missing":  # given an id
        from apps.store.models import Store

        v = Store.objects.filter(pk=store).values_list("points_per_ils", flat=True).first()
    return int(v) if v else POINTS_PER_ILS


def rate_for(store, amount) -> Decimal:
    """The earn rate, as a FRACTION (0.02 = 2%), for a receipt of `amount`.

    The store's bands (EarnRule) pick the rate for the WHOLE receipt: a
    ₪55 bill in a 50–100 → 5% band earns 5% on all ₪55, not 5% on the last
    five. No bands, or no band covering the amount → the default EARN_RATE.
    """
    if store is None or amount is None:
        return EARN_RATE
    from apps.store.models import EarnRule

    amt = Decimal(str(amount))
    sid = getattr(store, "pk", store)
    for rule in EarnRule.objects.for_pharmacy(sid).order_by("min_total", "position"):
        if amt >= rule.min_total and (rule.max_total is None or amt < rule.max_total):
            return Decimal(rule.rate_percent) / Decimal(100)
    if EarnRule.objects.for_pharmacy(sid).exists():
        # Bands are set but none covers this amount (a gap the owner left,
        # or below the first band): earn nothing rather than guess.
        return Decimal("0")
    return EARN_RATE


def points_for(amount, store=None, rate=None, per=None) -> int:
    """Points earned on `amount` shekels of CASH paid.

    FLOOR, once, at the end: ₪17.50 at 2% is 3.5 points → 3. Rounding up would
    mint value nobody paid for; rounding per line would compound it.
    """
    if amount is None:
        return 0
    amt = Decimal(str(amount))
    if amt <= 0:
        return 0
    r = rate if rate is not None else rate_for(store, amt)
    raw = amt * Decimal(r) * (per or per_ils(store))
    return int(raw.to_integral_value(rounding=ROUND_FLOOR))


def value_of(points: int, store=None, per=None) -> Decimal:
    """What `points` are worth, in shekels, at the store's CURRENT rate.
    For points already spent, read the bill's beans_value instead."""
    if not points or points <= 0:
        return Decimal("0.00")
    return (Decimal(int(points)) / Decimal(per or per_ils(store))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def max_spendable(balance: int, amount, store=None) -> int:
    """Most points that may be applied to a bill of `amount`.

    Points are only ever spent in WHOLE shekels: in steps of the store's rate
    (10, 20, 30… when 10 points = 1 ₪), so a redemption is never 55 points =
    5.50 ₪. The rest of the balance stays for next time.

    Bounded by BOTH the balance and the bill: points cannot buy more than the
    drink costs, and a redemption must never take a total below zero. Floor,
    not round — rounding up here would hand out value that was not earned.
    """
    if not balance or balance <= 0 or amount is None:
        return 0
    amt = Decimal(str(amount))
    if amt <= 0:
        return 0
    per = per_ils(store)
    shekels = min(int(balance) // per, int(amt.to_integral_value(rounding=ROUND_FLOOR)))
    return max(0, shekels * per)


def balance_of(customer) -> int:
    profile = getattr(customer, "loyalty", None)
    return int(getattr(profile, "beans", 0) or 0)


def cycle_of(kind: str, source: str, source_id) -> int:
    """How many times this movement has already happened for this thing.

    An order can now be collected, un-collected and collected again, and each
    of those has to move the balance — while a RETRIED request must still move
    it once. Those two requirements pull in opposite directions and the
    idempotency key is where they meet: it stays constant across retries of the
    same event and changes between genuine repeats of it.

    So the key carries a cycle number, counted from the ledger itself rather
    than from a column on Order, which keeps this a pure function of history
    and needs no migration. Cycle 0 keeps the ORIGINAL key shape, so every
    ledger row already written stays matched.
    """
    from django.db.models import Q

    from apps.store.models import BeanLedger

    base = f"{kind}:{source}:{source_id}"
    return (
        BeanLedger.objects.unscoped()
        .filter(Q(idempotency_key=base) | Q(idempotency_key__startswith=f"{base}#"))
        .count()
    )


def _key(kind: str, source: str, source_id, cycle: int) -> str:
    base = f"{kind}:{source}:{source_id}"
    return base if not cycle else f"{base}#{cycle}"


@transaction.atomic
def move(store, customer, delta: int, reason: str, note: str, idempotency_key: str,
         *, sale=None, rate_applied=None):
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
    # .unscoped() is REQUIRED here, and it is not the escape hatch it looks
    # like. TenantManager raises on any unscoped read; `select_for_update()`
    # is a read as far as it is concerned, so the plain manager form threw
    # TenantScopeError from inside every sale that had a customer attached —
    # a 500 on the till, with the sale stuck in the offline queue. The row
    # is still scoped: store and customer are both in the lookup.
    profile, _ = (
        LoyaltyProfile.objects.unscoped()
        .select_for_update()
        .get_or_create(store=store, customer=customer, defaults={"beans": 0})
    )
    have = int(profile.beans or 0)

    # A redemption can never take the balance negative, whatever the caller
    # believed the balance was when it built the request.
    if delta < 0 and have + delta < 0:
        delta = -have
        if delta == 0:
            return None, False

    row, created = BeanLedger.objects.unscoped().get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "store": store,
            "customer": customer,
            "delta": delta,
            "reason": reason,
            "balance_after": have + delta,
            "note": note or "",
            "sale": sale,
            "rate_applied": rate_applied,
        },
    )
    if not created:
        return row, False

    profile.beans = have + delta
    profile.save(update_fields=["beans"])
    return row, True


def award_for_purchase(store, customer, amount, *, source: str, source_id, cycle: int = 0, sale=None) -> int:
    """Mint points for a completed purchase. Returns the points awarded.

    Called when the goods are actually handed over — an order marked collected,
    or a sale rung up — never when an order is merely placed. An order that is
    cancelled must not have already paid out. The band's rate is stored on the
    ledger row so the receipt can explain itself later.
    """
    if customer is None:
        return 0
    rate = rate_for(store, amount)
    points = points_for(amount, store=store, rate=rate)
    if points <= 0:
        return 0

    from apps.store.models import BeanLedger

    _, applied = move(
        store, customer, points, BeanLedger.Reason.EARN,
        f"نقاط {source} #{source_id}",
        _key("earn", source, source_id, cycle),
        sale=sale,
        rate_applied=(rate * 100).quantize(Decimal("0.01")),
    )
    return points if applied else 0


def earned_on(sale) -> int:
    """Points this sale actually minted, net of earlier partial reversals."""
    from django.db.models import Sum

    from apps.store.models import BeanLedger

    rows = BeanLedger.objects.for_pharmacy(sale.store_id).filter(sale_id=sale.pk)
    earned = rows.filter(reason=BeanLedger.Reason.EARN).aggregate(n=Sum("delta"))["n"] or 0
    return int(earned)


def reverse_points(store, customer, points: int, *, source: str, source_id, key: str, sale=None) -> int:
    """Take back an exact number of points (a partial return). Clipped at the
    balance by move(): the shop eats points already spent."""
    if customer is None or not points or points <= 0:
        return 0
    from apps.store.models import BeanLedger

    row, applied = move(
        store, customer, -int(points), BeanLedger.Reason.ADJUST,
        f"إرجاع {source} #{source_id}", f"return:{key}", sale=sale,
    )
    return -int(row.delta) if (applied and row is not None) else 0


def spend_on_purchase(store, customer, points: int, amount, *, source: str, source_id, cycle: int = 0, sale=None) -> int:
    """Redeem points against a bill. Returns how many were actually spent.

    Clamped server-side against the live balance and the bill, because the
    number the phone or the till sent was true thirty seconds ago and may not
    be now.
    """
    if customer is None or not points or points <= 0:
        return 0

    from apps.store.models import BeanLedger

    spend = max_spendable(balance_of(customer), amount, store)
    per = per_ils(store)
    spend = (min(int(points), spend) // per) * per  # whole shekels only
    if spend <= 0:
        return 0

    _, applied = move(
        store, customer, -spend, BeanLedger.Reason.REDEEM,
        f"استبدال في {source} #{source_id}",
        _key("redeem", source, source_id, cycle),
        sale=sale,
    )
    return spend if applied else 0


def refund_spend(store, customer, points: int, *, source: str, source_id, cycle: int = 0) -> int:
    """Give back points spent on something that did not happen."""
    if customer is None or not points or points <= 0:
        return 0
    from apps.store.models import BeanLedger

    _, applied = move(
        store, customer, int(points), BeanLedger.Reason.ADJUST,
        f"إلغاء {source} #{source_id}",
        _key("refund", source, source_id, cycle),
    )
    return int(points) if applied else 0


def reverse_award(store, customer, amount, *, source: str, source_id, cycle: int = 0) -> int:
    """Take back points minted for a purchase that was then returned.

    Deliberately allowed to be clipped by `move()` if the customer has already
    spent them: the shop eats the difference rather than putting someone into a
    negative balance they cannot understand or clear.
    """
    if customer is None:
        return 0
    from apps.store.models import BeanLedger

    # Take back exactly what was minted (the rate or bands may have changed
    # since); recompute only if no award row exists.
    minted = (
        BeanLedger.objects.unscoped()
        .filter(idempotency_key=_key("earn", source, source_id, cycle))
        .values_list("delta", flat=True).first()
    )
    points = int(minted) if minted else points_for(amount, store=store)
    if points <= 0:
        return 0

    _, applied = move(
        store, customer, -points, BeanLedger.Reason.ADJUST,
        f"إرجاع {source} #{source_id}",
        _key("reverse", source, source_id, cycle),
    )
    return points if applied else 0


def totals_for(store, customer) -> dict:
    """Everything earned and everything spent, from the ledger.

    Read off the ledger rather than off `LoyaltyProfile`, because the profile
    only carries the running balance: it cannot answer "how many have they used
    so far", which is the question a barista actually gets asked. Two signed
    sums over rows that are already indexed by (store, customer).
    """
    from django.db.models import Q, Sum
    from django.db.models.functions import Coalesce

    from apps.store.models import BeanLedger

    if customer is None:
        return {"balance": 0, "earned": 0, "spent": 0, "redemptions": 0}

    agg = (
        BeanLedger.objects.for_pharmacy(getattr(store, "pk", store))
        .filter(customer=customer)
        .aggregate(
            earned=Coalesce(Sum("delta", filter=Q(delta__gt=0)), 0),
            # Stored negative; reported as a positive count of points used.
            spent=Coalesce(Sum("delta", filter=Q(delta__lt=0)), 0),
        )
    )
    redemptions = (
        BeanLedger.objects.for_pharmacy(getattr(store, "pk", store))
        .filter(customer=customer, reason=BeanLedger.Reason.REDEEM)
        .count()
    )
    return {
        "balance": balance_of(customer),
        "earned": int(agg["earned"] or 0),
        "spent": -int(agg["spent"] or 0),
        "redemptions": redemptions,
    }


def adjust(store, customer, delta: int, note: str, *, key: str) -> int:
    """A human moving somebody's points by hand. Returns what actually moved.

    The counter needs this for the cases the automatic rules cannot see: a
    drink remade, an apology, a card handed over before the app existed, a
    mistake to undo. It is deliberately NOT a setter — you say "+20" or "-20",
    never "= 40" — so the ledger stays a story of movements and the balance
    stays derivable from it.

    `key` is the caller's idempotency key. A cashier double-tapping the button
    must not credit twice; two DIFFERENT adjustments of the same size on the
    same day must both land. Only the caller knows which is which, so only the
    caller can supply it.
    """
    from apps.store.models import BeanLedger

    if customer is None or not delta:
        return 0
    row, applied = move(
        store, customer, int(delta), BeanLedger.Reason.ADJUST,
        (note or "").strip()[:255] or "تعديل يدوي",
        f"adjust:{key}",
    )
    if not applied or row is None:
        return 0
    # move() clips a negative movement at the balance, so report the ledger
    # row's delta — what happened — not the delta that was asked for.
    return int(row.delta)

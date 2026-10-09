"""The till's cash drawer: open with a count, close with a count, and the
books say what should be in between.

    GET  /api/v1/cash-drawer/          the open drawer (or none) + recent closes
    POST /api/v1/cash-drawer/open/     {opening_amount}
    POST /api/v1/cash-drawer/move/     {amount, direction: in|out, note}
    POST /api/v1/cash-drawer/close/    {counted_amount, note}

What SHOULD be in the drawer is never typed — it is worked out:

    opening + cash sales − cash refunds + cash put in − cash taken out

Card sales are shown beside it but never counted: that money is not in the
drawer. An employee counts BLIND — they see what is expected only after they
have typed what they counted (the owner sees it live), so the count is a
count and not a copy of the screen.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import ModuleEnabled, StoreResolved
from apps.store import models
from apps.store.views import request_pharmacy_id

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def _is_owner(user) -> bool:
    return bool(user and (user.is_superuser or getattr(user, "role", "") == "owner"))


def _money(value, field: str, allow_zero: bool = True) -> Decimal:
    try:
        v = Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError({field: "أدخل مبلغاً صحيحاً."})
    if v < 0 or (v == 0 and not allow_zero) or v > Decimal("1000000"):
        raise ValidationError({field: "أدخل مبلغاً صحيحاً."})
    return v


def _name(user) -> str:
    if user is None:
        return ""
    return (getattr(user, "display_name", "") or "").strip() or user.get_username()


def figures(session: models.CashSession, until=None) -> dict:
    """Everything that moved cash in or out of the drawer while it was open."""
    lo = session.opened_at
    hi = session.closed_at or until or timezone.now()
    sid = session.store_id
    sales = models.Sale.objects.for_pharmacy(sid).filter(created_at__gte=lo, created_at__lt=hi)
    by = lambda qs, f: qs.aggregate(n=Sum(f), c=Count("id"))  # noqa: E731

    cash = by(sales.filter(is_return=False, payment_method="cash"), "discounted_total")
    card = by(sales.filter(is_return=False, payment_method="card"), "discounted_total")
    # Money handed back over the counter: returns against cash sales, plus
    # any old-style return invoice paid in cash.
    refunds = (
        models.SaleReturn.objects.for_pharmacy(sid)
        .filter(created_at__gte=lo, created_at__lt=hi, sale__payment_method="cash")
        .aggregate(n=Sum("refund_amount"))["n"]
        or ZERO
    ) + (sales.filter(is_return=True, payment_method="cash").aggregate(n=Sum("discounted_total"))["n"] or ZERO)
    moves = models.CashMove.objects.for_pharmacy(sid).filter(session=session)
    put_in = moves.filter(amount__gt=0).aggregate(n=Sum("amount"))["n"] or ZERO
    took_out = -(moves.filter(amount__lt=0).aggregate(n=Sum("amount"))["n"] or ZERO)

    opening = session.opening_amount or ZERO
    cash_sales = cash["n"] or ZERO
    expected = (opening + cash_sales - refunds + put_in - took_out).quantize(CENT)
    return {
        "opening": str(opening.quantize(CENT)),
        "cash_sales": str(cash_sales.quantize(CENT)),
        "cash_tickets": cash["c"] or 0,
        "card_sales": str((card["n"] or ZERO).quantize(CENT)),
        "card_tickets": card["c"] or 0,
        "refunds": str(refunds.quantize(CENT)),
        "put_in": str(put_in.quantize(CENT)),
        "took_out": str(took_out.quantize(CENT)),
        "expected": str(expected),
    }


def payload(session: models.CashSession | None, *, owner: bool, reveal: bool = False) -> dict | None:
    """A session for the API. Live figures go to the owner only, until the
    drawer is closed (or `reveal` — the person who just closed it)."""
    if session is None:
        return None
    closed = session.closed_at is not None
    out = {
        "id": session.pk,
        "opened_at": session.opened_at.isoformat(),
        "opened_by": _name(session.opened_by),
        "opening_amount": str(session.opening_amount),
        "closed_at": session.closed_at.isoformat() if closed else None,
        "closed_by": _name(session.closed_by) if closed else None,
        "note": session.note,
        "moves": [
            {
                "amount": str(m.amount),
                "note": m.note,
                "at": m.created_at.isoformat(),
                "by": _name(m.created_by),
            }
            for m in session.moves.all().select_related("created_by")
        ],
    }
    if owner or reveal:
        f = figures(session)
        if closed and session.expected_amount is not None:
            f["expected"] = str(session.expected_amount)  # frozen at closing
        out["figures"] = f
    if closed and (owner or reveal):
        counted = session.counted_amount or ZERO
        expected = session.expected_amount or ZERO
        out["counted_amount"] = str(counted)
        out["expected_amount"] = str(expected)
        out["difference"] = str((counted - expected).quantize(CENT))
    return out


class _Base(APIView):
    permission_classes = [permissions.IsAuthenticated, StoreResolved, ModuleEnabled]
    required_module = "pos"

    def _open(self, sid, lock=False):
        if lock:
            # No select_related under FOR UPDATE: Postgres refuses to lock the
            # nullable side of an outer join.
            return (
                models.CashSession.objects.unscoped().select_for_update()
                .filter(store_id=sid, closed_at__isnull=True).first()
            )
        return (
            models.CashSession.objects.for_pharmacy(sid).filter(closed_at__isnull=True)
            .select_related("opened_by").first()
        )


class CashDrawerView(_Base):
    def get(self, request):
        sid = request_pharmacy_id(request)
        owner = _is_owner(request.user)
        history = []
        if owner:
            history = [
                payload(s, owner=True)
                for s in models.CashSession.objects.for_pharmacy(sid)
                .filter(closed_at__isnull=False)
                .select_related("opened_by", "closed_by")
                .order_by("-closed_at")[:15]
            ]
        return Response({"open": payload(self._open(sid), owner=owner), "history": history})


class CashDrawerOpenView(_Base):
    def post(self, request):
        sid = request_pharmacy_id(request)
        amount = _money(request.data.get("opening_amount", "0"), "opening_amount")
        try:
            with transaction.atomic():
                if self._open(sid, lock=True):
                    raise ValidationError({"detail": "الصندوق مفتوح بالفعل — أغلقه أولاً."})
                s = models.CashSession.objects.create(
                    store_id=sid, opened_by=request.user, opened_at=timezone.now(), opening_amount=amount,
                )
        except IntegrityError:
            raise ValidationError({"detail": "الصندوق مفتوح بالفعل — أغلقه أولاً."})
        return Response(payload(s, owner=_is_owner(request.user)), status=201)


class CashDrawerMoveView(_Base):
    def post(self, request):
        sid = request_pharmacy_id(request)
        amount = _money(request.data.get("amount"), "amount", allow_zero=False)
        direction = request.data.get("direction")
        if direction not in ("in", "out"):
            raise ValidationError({"direction": "إدخال أو إخراج؟"})
        note = (request.data.get("note") or "").strip()[:255]
        if direction == "out" and not note:
            raise ValidationError({"note": "اكتب لماذا أُخرج المبلغ (مثال: شراء حليب)."})
        with transaction.atomic():
            s = self._open(sid, lock=True)
            if s is None:
                raise ValidationError({"detail": "افتح الصندوق أولاً."})
            models.CashMove.objects.create(
                store_id=sid, session=s, amount=amount if direction == "in" else -amount,
                note=note, created_by=request.user,
            )
        return Response(payload(s, owner=_is_owner(request.user)), status=201)


class CashDrawerCloseView(_Base):
    def post(self, request):
        sid = request_pharmacy_id(request)
        counted = _money(request.data.get("counted_amount"), "counted_amount")
        note = (request.data.get("note") or "").strip()[:255]
        with transaction.atomic():
            s = self._open(sid, lock=True)
            if s is None:
                raise ValidationError({"detail": "الصندوق غير مفتوح."})
            now = timezone.now()
            s.closed_at = now
            s.closed_by = request.user
            s.counted_amount = counted
            s.expected_amount = Decimal(figures(s, until=now)["expected"])
            s.note = note
            s.save(update_fields=["closed_at", "closed_by", "counted_amount", "expected_amount", "note", "updated_at"])
        s = models.CashSession.objects.for_pharmacy(sid).select_related("opened_by", "closed_by").get(pk=s.pk)
        # Whoever counted sees the answer — that is the point of counting.
        return Response(payload(s, owner=_is_owner(request.user), reveal=True))

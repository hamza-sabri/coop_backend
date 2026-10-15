"""The till's cash drawer: open with a count, close with a count, and the
books say what should be in between.

    GET   /api/v1/cash-drawer/            the open drawer (or none) + recent closes
    POST  /api/v1/cash-drawer/open/       {counts | opening_amount, opened_by?}
    POST  /api/v1/cash-drawer/move/       {amount, direction: in|out, note}
    POST  /api/v1/cash-drawer/close/      {counts | counted_amount, card_counted?, closed_by?, note}
    GET   /api/v1/cash-drawer/history/    closed sessions, newest first, ?page=
    GET   /api/v1/cash-drawer/<id>/       one session with every operation (printable)
    PATCH /api/v1/cash-drawer/<id>/       {opened_by?, closed_by?, note?} — owner

What SHOULD be in the drawer is never typed — it is worked out:

    opening + cash sales − cash refunds + cash put in − cash taken out

Card sales are shown beside it but never counted: that money is not in the
drawer. At closing the person types what the card terminal's total says, and
the two are compared too. A count is entered as notes and coins (how many
200s, how many 10s…), and the sum is computed here; a plain amount is still
accepted for a till without the new screen.

An employee counts BLIND — they see what is expected only after they have
typed what they counted (the owner sees it live), so the count is a count and
not a copy of the screen.

Who opened and who closed is whoever did it at the counter, which is not
always the account that is signed in (staff share a till and rarely sign out),
so the person can be picked from the store's staff; the owner can correct it
afterwards.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import permissions
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import ModuleEnabled, StoreResolved
from apps.store import models
from apps.store.views import request_pharmacy_id

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

#: The notes and coins in circulation, largest first. A count is a dict of
#: these keys to how many; anything else is refused.
DENOMINATIONS = ("200", "100", "50", "20", "10", "5", "2", "1", "0.5", "0.1")


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


def _counts(raw) -> tuple[dict, Decimal]:
    """{"200": 3, "0.5": 12} → (cleaned dict, total). Empty dict → 0."""
    if not raw:
        return {}, ZERO
    if not isinstance(raw, dict):
        raise ValidationError({"counts": "أدخل عدد كل فئة."})
    out, total = {}, ZERO
    for k, n in raw.items():
        key = str(k).strip()
        if key not in DENOMINATIONS:
            raise ValidationError({"counts": f"فئة غير معروفة: {key}"})
        try:
            cnt = int(n or 0)
        except (TypeError, ValueError):
            raise ValidationError({"counts": "أدخل عدداً صحيحاً لكل فئة."})
        if cnt < 0 or cnt > 100000:
            raise ValidationError({"counts": "أدخل عدداً صحيحاً لكل فئة."})
        if cnt:
            out[key] = cnt
            total += Decimal(key) * cnt
    return out, total.quantize(CENT)


def _amount_or_counts(data, field: str) -> tuple[Decimal, dict]:
    """The new screen sends `counts`; an older till sends the amount."""
    counts, total = _counts(data.get("counts"))
    if counts or data.get(field) in (None, ""):
        return total, counts
    return _money(data.get(field, "0"), field), {}


def _name(user) -> str:
    if user is None:
        return ""
    return (getattr(user, "display_name", "") or "").strip() or user.get_username()


def _staff(sid, user_id):
    """A user id from the request → that store's staff member, or 400."""
    if user_id in (None, ""):
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise ValidationError({"user": "اختر موظفاً."})
    u = get_user_model().objects.filter(pk=uid, store_id=sid, is_active=True).first()
    if u is None:
        raise ValidationError({"user": "اختر موظفاً من هذا المحل."})
    return u


def figures(session: models.CashSession, until=None) -> dict:
    """Everything that moved cash in or out of the drawer while it was open."""
    lo = session.opened_at
    hi = session.closed_at or until or timezone.now()
    sid = session.store_id
    sales = models.Sale.objects.for_pharmacy(sid).filter(created_at__gte=lo, created_at__lt=hi)
    by = lambda qs, f: qs.aggregate(n=Sum(f), c=Count("id"))  # noqa: E731

    cash = by(sales.filter(is_return=False, payment_method="cash"), "discounted_total")
    card = by(sales.filter(is_return=False, payment_method="card"), "discounted_total")
    debt = by(sales.filter(is_return=False, payment_method="debt"), "discounted_total")
    # Money handed back over the counter: returns against cash sales, plus
    # any old-style return invoice paid in cash.
    cash_refunds = (
        models.SaleReturn.objects.for_pharmacy(sid)
        .filter(created_at__gte=lo, created_at__lt=hi, sale__payment_method="cash")
        .aggregate(n=Sum("refund_amount"))["n"]
        or ZERO
    ) + (sales.filter(is_return=True, payment_method="cash").aggregate(n=Sum("discounted_total"))["n"] or ZERO)
    card_refunds = (
        models.SaleReturn.objects.for_pharmacy(sid)
        .filter(created_at__gte=lo, created_at__lt=hi, sale__payment_method="card")
        .aggregate(n=Sum("refund_amount"))["n"]
        or ZERO
    ) + (sales.filter(is_return=True, payment_method="card").aggregate(n=Sum("discounted_total"))["n"] or ZERO)
    moves = models.CashMove.objects.for_pharmacy(sid).filter(session=session)
    put_in = moves.filter(amount__gt=0).aggregate(n=Sum("amount"))["n"] or ZERO
    took_out = -(moves.filter(amount__lt=0).aggregate(n=Sum("amount"))["n"] or ZERO)

    opening = session.opening_amount or ZERO
    cash_sales = cash["n"] or ZERO
    card_sales = card["n"] or ZERO
    expected = (opening + cash_sales - cash_refunds + put_in - took_out).quantize(CENT)
    q = lambda v: str((v or ZERO).quantize(CENT))  # noqa: E731
    return {
        "opening": q(opening),
        "cash_sales": q(cash_sales),
        "cash_tickets": cash["c"] or 0,
        "card_sales": q(card_sales),
        "card_tickets": card["c"] or 0,
        "debt_sales": q(debt["n"]),
        "debt_tickets": debt["c"] or 0,
        "refunds": q(cash_refunds),  # kept name: cash handed back
        "card_refunds": q(card_refunds),
        "put_in": q(put_in),
        "took_out": q(took_out),
        "expected": str(expected),
        "card_expected": q(card_sales - card_refunds),
        "total_sales": q(cash_sales + card_sales + (debt["n"] or ZERO)),
        "tickets": (cash["c"] or 0) + (card["c"] or 0) + (debt["c"] or 0),
    }


def operations(session: models.CashSession) -> list[dict]:
    """Every operation while the drawer was open, in time order: sales,
    returns, cash in and out. This is what the printed record lists."""
    lo = session.opened_at
    hi = session.closed_at or timezone.now()
    sid = session.store_id
    rows = []
    sales = (
        models.Sale.objects.for_pharmacy(sid)
        .filter(created_at__gte=lo, created_at__lt=hi)
        .select_related("customer", "created_by")
        .order_by("created_at")
    )
    for s in sales:
        rows.append({
            "at": s.created_at.isoformat(),
            "kind": "return" if s.is_return else "sale",
            "payment": s.payment_method,
            "ref": s.receipt_code or str(s.pk),
            "sale_id": s.pk,
            "amount": str((-s.discounted_total if s.is_return else s.discounted_total).quantize(CENT)),
            "who": _name(s.created_by),
            "customer": s.customer.name if s.customer_id else "",
            "note": "",
        })
    returns = (
        models.SaleReturn.objects.for_pharmacy(sid)
        .filter(created_at__gte=lo, created_at__lt=hi)
        .select_related("sale", "created_by")
        .order_by("created_at")
    )
    for r in returns:
        rows.append({
            "at": r.created_at.isoformat(),
            "kind": "refund",
            "payment": r.sale.payment_method,
            "ref": r.sale.receipt_code or str(r.sale_id),
            "sale_id": r.sale_id,
            "amount": str((-(r.refund_amount or ZERO)).quantize(CENT)),
            "who": _name(getattr(r, "created_by", None)),
            "customer": "",
            "note": (getattr(r, "reason", "") or "")[:80],
        })
    for m in session.moves.all().select_related("created_by"):
        rows.append({
            "at": m.created_at.isoformat(),
            "kind": "in" if m.amount > 0 else "out",
            "payment": "cash",
            "ref": "",
            "sale_id": None,
            "amount": str(m.amount.quantize(CENT)),
            "who": _name(m.created_by),
            "customer": "",
            "note": m.note,
        })
    rows.sort(key=lambda r: r["at"])
    return rows


def payload(session: models.CashSession | None, *, owner: bool, reveal: bool = False, detail: bool = False) -> dict | None:
    """A session for the API. Live figures go to the owner only, until the
    drawer is closed (or `reveal` — the person who just closed it)."""
    if session is None:
        return None
    closed = session.closed_at is not None
    out = {
        "id": session.pk,
        "opened_at": session.opened_at.isoformat(),
        "opened_by": _name(session.opened_by),
        "opened_by_id": session.opened_by_id,
        "opening_amount": str(session.opening_amount),
        "opening_counts": session.opening_counts or {},
        "closed_at": session.closed_at.isoformat() if closed else None,
        "closed_by": _name(session.closed_by) if closed else None,
        "closed_by_id": session.closed_by_id if closed else None,
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
        if closed and session.closing_figures:
            f = dict(session.closing_figures)  # frozen at closing
        else:
            f = figures(session)
            if closed and session.expected_amount is not None:
                f["expected"] = str(session.expected_amount)
        out["figures"] = f
    if closed and (owner or reveal):
        counted = session.counted_amount or ZERO
        expected = session.expected_amount or ZERO
        out["counted_amount"] = str(counted)
        out["closing_counts"] = session.closing_counts or {}
        out["expected_amount"] = str(expected)
        out["difference"] = str((counted - expected).quantize(CENT))
        if session.card_counted is not None:
            out["card_counted"] = str(session.card_counted)
            card_expected = Decimal(out["figures"].get("card_expected", "0"))
            out["card_difference"] = str((session.card_counted - card_expected).quantize(CENT))
    if detail and (owner or reveal):
        out["operations"] = operations(session)
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
        return Response({"open": payload(self._open(sid), owner=owner), "history": history, "denominations": list(DENOMINATIONS)})


class CashDrawerOpenView(_Base):
    def post(self, request):
        sid = request_pharmacy_id(request)
        amount, counts = _amount_or_counts(request.data, "opening_amount")
        who = _staff(sid, request.data.get("opened_by")) or request.user
        try:
            with transaction.atomic():
                if self._open(sid, lock=True):
                    raise ValidationError({"detail": "الصندوق مفتوح بالفعل — أغلقه أولاً."})
                s = models.CashSession.objects.create(
                    store_id=sid, opened_by=who, opened_at=timezone.now(),
                    opening_amount=amount, opening_counts=counts,
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
        counted, counts = _amount_or_counts(request.data, "counted_amount")
        card = request.data.get("card_counted")
        card_counted = _money(card, "card_counted") if card not in (None, "") else None
        who = _staff(sid, request.data.get("closed_by")) or request.user
        note = (request.data.get("note") or "").strip()[:255]
        with transaction.atomic():
            s = self._open(sid, lock=True)
            if s is None:
                raise ValidationError({"detail": "الصندوق غير مفتوح."})
            now = timezone.now()
            f = figures(s, until=now)
            s.closed_at = now
            s.closed_by = who
            s.counted_amount = counted
            s.closing_counts = counts
            s.card_counted = card_counted
            s.expected_amount = Decimal(f["expected"])
            s.closing_figures = f
            s.note = note
            s.save(update_fields=[
                "closed_at", "closed_by", "counted_amount", "closing_counts", "card_counted",
                "expected_amount", "closing_figures", "note", "updated_at",
            ])
        s = models.CashSession.objects.for_pharmacy(sid).select_related("opened_by", "closed_by").get(pk=s.pk)
        # Whoever counted sees the answer — that is the point of counting.
        return Response(payload(s, owner=_is_owner(request.user), reveal=True))


class CashDrawerHistoryView(_Base):
    """Closed sessions, newest first. Owner only: the differences are the
    shop's money. ?page=1 (20 per page)."""

    def get(self, request):
        sid = request_pharmacy_id(request)
        if not _is_owner(request.user):
            raise PermissionDenied("سجل الصندوق للمالك فقط.")
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except ValueError:
            page = 1
        size = 20
        qs = (
            models.CashSession.objects.for_pharmacy(sid)
            .filter(closed_at__isnull=False)
            .select_related("opened_by", "closed_by")
            .order_by("-closed_at")
        )
        total = qs.count()
        rows = [payload(s, owner=True) for s in qs[(page - 1) * size: page * size]]
        return Response({"count": total, "page": page, "page_size": size, "results": rows})


class CashDrawerDetailView(_Base):
    """One session, with every operation — the printable record.
    Owners see any; an employee sees only a session they opened or closed."""

    def _get(self, request):
        sid = request_pharmacy_id(request)
        s = (
            models.CashSession.objects.for_pharmacy(sid)
            .select_related("opened_by", "closed_by")
            .filter(pk=self.kwargs.get("pk")).first()
        )
        if s is None:
            raise NotFound("لا يوجد سجل بهذا الرقم.")
        owner = _is_owner(request.user)
        mine = request.user.pk in (s.opened_by_id, s.closed_by_id)
        if not (owner or mine):
            raise PermissionDenied("هذا السجل للمالك ومن فتح الصندوق أو أغلقه.")
        return s, owner, mine

    def get(self, request, pk=None):
        s, owner, mine = self._get(request)
        return Response(payload(s, owner=owner, reveal=mine and s.closed_at is not None, detail=True))

    def patch(self, request, pk=None):
        """Correct who opened or closed (staff share a till), or the note."""
        s, owner, _ = self._get(request)
        if not owner:
            raise PermissionDenied("تعديل السجل للمالك فقط.")
        sid = s.store_id
        fields = []
        if "opened_by" in request.data:
            s.opened_by = _staff(sid, request.data.get("opened_by"))
            fields.append("opened_by")
        if "closed_by" in request.data:
            if s.closed_at is None:
                raise ValidationError({"closed_by": "الصندوق لم يُغلق بعد."})
            s.closed_by = _staff(sid, request.data.get("closed_by"))
            fields.append("closed_by")
        if "note" in request.data:
            s.note = (request.data.get("note") or "").strip()[:255]
            fields.append("note")
        if fields:
            s.save(update_fields=fields + ["updated_at"])
        s = models.CashSession.objects.for_pharmacy(sid).select_related("opened_by", "closed_by").get(pk=s.pk)
        return Response(payload(s, owner=True, detail=True))

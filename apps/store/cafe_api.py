"""The café's back office: returns, raw-material inventory, shifts, expenses,
points bands and the P&L.

Kept out of views.py (already 4,800 lines) so each piece reads on its own.
Every viewset is tenant-scoped through StoreScopedMixin; money that only the
owner may see is gated server-side here, not merely hidden in the UI.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import ModuleEnabled, OwnerRequired, StoreResolved
from apps.store import finance, models
from apps.store import points as points_service
from apps.store.serializers import OwnerOnlyFieldsMixin, _viewer_is_owner
from apps.store.views import (
    ReportsBaseView,
    StoreScopedMixin,
    invalidate_reports_cache,
    invalidate_sales_stats_cache,
    request_pharmacy_id,
)

CENT = Decimal("0.01")


def _is_owner(user) -> bool:
    return bool(user and (user.is_superuser or getattr(user, "role", "") == "owner"))


class OwnerWritesMixin:
    """Everyone signed in may read; only the owner may change."""

    def get_permissions(self):
        perms = super().get_permissions()
        if self.request.method not in permissions.SAFE_METHODS:
            perms.append(OwnerRequired())
        return perms


class OwnerOnlyMixin:
    def get_permissions(self):
        perms = super().get_permissions()
        perms.append(OwnerRequired())
        return perms


# ═══════════════════════════════════════════════════════════════════════════
# Returns
# ═══════════════════════════════════════════════════════════════════════════
class SaleReturnInput(serializers.Serializer):
    sale_item = serializers.IntegerField()
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001"), required=False
    )
    #: "full" refunds the line's share of what was paid; "none" is a remake.
    refund = serializers.ChoiceField(choices=["full", "none"], default="full")
    reason = serializers.ChoiceField(choices=models.SaleReturn.Reason.choices)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)
    client_uuid = serializers.CharField(max_length=64, required=False, allow_blank=True)


def record_return(sale, data, user) -> models.SaleReturn:
    """Hand back (part of) one line. Idempotent on client_uuid.

    Money: the line's share of the CASH paid, scaled by quantity. Points: the
    sale's earned points are taken back in the same proportion (floor), and
    points the customer spent on it come back in the same proportion, so a
    full refund leaves them exactly where they were before the line.
    """
    store_id = sale.store_id
    cu = (data.get("client_uuid") or "").strip() or None
    if cu:
        found = models.SaleReturn.objects.for_pharmacy(store_id).filter(client_uuid=cu).first()
        if found:
            return found

    with transaction.atomic():
        locked = models.Sale.objects.for_pharmacy(store_id).select_for_update().get(pk=sale.pk)
        if locked.is_return:
            raise ValidationError({"detail": "هذه فاتورة إرجاع أصلاً."})
        line = models.SaleItem.objects.for_pharmacy(store_id).filter(
            sale_id=locked.pk, pk=data["sale_item"]
        ).first()
        if line is None:
            raise ValidationError({"sale_item": "الصنف ليس في هذه الفاتورة."})

        already = models.SaleReturn.objects.for_pharmacy(store_id).filter(
            sale_item_id=line.pk
        ).aggregate(n=Sum("quantity"))["n"] or Decimal("0")
        remaining = Decimal(line.quantity) - Decimal(already)
        qty = Decimal(data.get("quantity") or remaining)
        if remaining <= 0:
            raise ValidationError({"quantity": "هذا الصنف أُرجع بالكامل."})
        if qty > remaining:
            raise ValidationError({"quantity": f"المتبقي للإرجاع {remaining.normalize()} فقط."})

        # The line's share of the bill, then of the line.
        total = Decimal(locked.total or 0)
        line_share = (Decimal(line.line_total) / total) if total > 0 else Decimal("0")
        qty_share = qty / Decimal(line.quantity)
        share = line_share * qty_share

        refund = Decimal("0.00")
        points_back = 0
        points_reversed = 0
        if data.get("refund", "full") == "full":
            refund = (Decimal(locked.discounted_total or 0) * share).quantize(CENT)
            # Never refund more than is left un-refunded on the sale.
            refunded = models.SaleReturn.objects.for_pharmacy(store_id).filter(
                sale_id=locked.pk
            ).aggregate(n=Sum("refund_amount"))["n"] or Decimal("0")
            refund = max(Decimal("0.00"), min(refund, Decimal(locked.discounted_total or 0) - refunded))

            if locked.customer_id:
                earned = points_service.earned_on(locked)
                take = int((Decimal(earned) * share).to_integral_value(rounding=ROUND_FLOOR))
                spent_back = int(
                    (Decimal(locked.beans_spent or 0) * share).to_integral_value(rounding=ROUND_FLOOR)
                )
                ret_key = cu or f"{locked.pk}:{line.pk}:{already}"
                if take > 0:
                    points_reversed = points_service.reverse_points(
                        locked.store, locked.customer, take,
                        source="بيع", source_id=locked.pk, key=f"{ret_key}:earn", sale=locked,
                    )
                if spent_back > 0:
                    points_back = points_service.adjust(
                        locked.store, locked.customer, spent_back,
                        f"إرجاع نقاط مستخدمة في بيع #{locked.pk}", key=f"return-spent:{ret_key}",
                    )

        cost = (Decimal(line.unit_cost) * qty).quantize(CENT) if line.unit_cost is not None else Decimal("0.00")
        try:
            row = models.SaleReturn.objects.create(
                store_id=store_id,
                sale=locked,
                sale_item=line,
                item_name=" — ".join(x for x in [line.medication_name, line.variant_label] if x),
                quantity=qty,
                refund_amount=refund,
                cost_written_off=cost,
                points_reversed=points_reversed - points_back,
                reason=data["reason"],
                note=(data.get("note") or "").strip()[:255],
                created_by=user if getattr(user, "is_authenticated", False) else None,
                client_uuid=cu,
            )
        except IntegrityError:
            found = models.SaleReturn.objects.for_pharmacy(store_id).filter(client_uuid=cu).first()
            if found:
                return found
            raise
        if row.refund_amount == 0:
            # A remake: a second drink was made, from the shelf.
            from apps.store import recipes

            recipes.consume_remake(row, user=user)
    return row


# ═══════════════════════════════════════════════════════════════════════════
# Inventory
# ═══════════════════════════════════════════════════════════════════════════
class InventoryItemSerializer(OwnerOnlyFieldsMixin, serializers.ModelSerializer):
    owner_only_fields = ("purchase_cost", "unit_cost", "stock_value")
    stock_value = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    unit_label = serializers.CharField(source="get_unit_display", read_only=True)
    purchase_unit_label = serializers.CharField(source="get_purchase_unit_display", read_only=True)
    state = serializers.SerializerMethodField()
    #: Base units used per day (sales, remakes, waste) over the last 14 days,
    #: and how many days the shelf lasts at that rate. Null = not used lately.
    daily_use = serializers.SerializerMethodField()
    days_left = serializers.SerializerMethodField()
    #: Opening stock on create, in the PURCHASE unit. Write-only convenience so
    #: "I have 2 crates now" is one form, not a form and a count.
    opening_stock = serializers.DecimalField(
        max_digits=14, decimal_places=3, write_only=True, required=False, min_value=Decimal("0")
    )

    class Meta:
        model = models.InventoryItem
        fields = [
            "id", "name", "category", "unit", "unit_label",
            "purchase_qty", "purchase_unit", "purchase_unit_label", "purchase_cost",
            "unit_cost", "stock", "stock_value", "reorder_level", "expiry_date",
            "supplier", "notes", "is_active", "client_uuid", "state",
            "daily_use", "days_left",
            "opening_stock", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "unit", "unit_cost", "stock", "created_at", "updated_at"]

    def _use(self, obj):
        usage = self.context.get("usage")
        if usage is None:
            from apps.store.breakdowns import daily_use

            usage = self.context["usage"] = daily_use(obj.store_id)
        return usage.get(obj.pk)

    def get_daily_use(self, obj):
        u = self._use(obj)
        return str(u.quantize(Decimal("0.001"))) if u else None

    def get_days_left(self, obj):
        from apps.store.breakdowns import days_left

        return days_left(obj.stock, self._use(obj))

    def get_state(self, obj) -> list:
        tags = []
        stock = Decimal(obj.stock or 0)
        if stock < 0:
            # Sales took more than the system knew was there: a delivery not
            # booked, or a count due. The till never blocks on it.
            tags.append("negative")
        if stock <= 0:
            tags.append("out")
        elif obj.reorder_level and stock <= Decimal(obj.reorder_level):
            tags.append("low")
        if obj.expiry_date:
            days = (obj.expiry_date - finance.today()).days
            if days < 0:
                tags.append("expired")
            elif days <= 7:
                tags.append("expiring")
        return tags


class InventoryItemViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    """Raw materials. Employees see counts and can record waste and counts;
    costs, purchases and deletes are the owner's."""

    queryset = models.InventoryItem.objects.unscoped()
    serializer_class = InventoryItemSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleEnabled]
    required_module = "stock"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    OWNER_ACTIONS = {"destroy", "purchase", "insights"}

    def get_permissions(self):
        perms = super().get_permissions()
        if getattr(self, "action", None) in self.OWNER_ACTIONS:
            perms.append(OwnerRequired())
        return perms

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        if p.get("search"):
            qs = qs.filter(Q(name__icontains=p["search"]) | Q(category__icontains=p["search"]) | Q(supplier__icontains=p["search"]))
        if p.get("category"):
            qs = qs.filter(category=p["category"])
        if p.get("active") != "all":
            qs = qs.filter(is_active=True)
        return qs.order_by("category", "name")

    def perform_create(self, serializer):
        cu = (serializer.validated_data.get("client_uuid") or "").strip() or None
        if cu:
            found = models.InventoryItem.objects.for_pharmacy(self.store_id).filter(client_uuid=cu).first()
            if found:
                serializer.instance = found
                return
        opening = serializer.validated_data.pop("opening_stock", None)
        if not _is_owner(self.request.user):
            serializer.validated_data.pop("purchase_cost", None)
        item = serializer.save(store_id=self.store_id)
        if opening:
            base = models.InventoryItem.to_base(opening, item.purchase_unit)
            # An opening balance is not a count difference: ADJUST, so the
            # P&L never reads the first stock on the shelf as a gain.
            _apply_move(item, models.StockMove.Kind.ADJUST, base, self.request.user,
                        reason="رصيد افتتاحي")

    def perform_update(self, serializer):
        serializer.validated_data.pop("opening_stock", None)
        serializer.save()

    def perform_destroy(self, instance):
        used = list(
            instance.recipe_lines.select_related("product").values_list("product__name", flat=True).distinct()[:5]
        )
        if used:
            raise ValidationError(
                {"detail": "الصنف داخل في وصفة: " + "، ".join(used) + ". احذفه من الوصفات أولاً، أو أوقفه بدل حذفه."}
            )
        instance.delete()

    @action(detail=True, methods=["get"])
    def ledger(self, request, pk=None):
        """The item's statement for a period — the answer to "where did it
        go?". Opening + every movement = closing, exactly; "used by sales" is
        broken down by drink. ?period=day|week|month&date= like the reports."""
        from apps.store import breakdowns

        item = self.get_object()
        r = finance.resolve_range(request.query_params)
        return Response(breakdowns.item_ledger(item, r["start"], r["end"], owner=_is_owner(request.user)))

    @action(detail=True, methods=["get"])
    def moves(self, request, pk=None):
        item = self.get_object()
        rows = item.moves.all().select_related("created_by")[:200]
        return Response([_move_json(m, _is_owner(request.user)) for m in rows])

    @action(detail=True, methods=["post"])
    def purchase(self, request, pk=None):
        """Stock arrived. `quantity` in `unit` (piece/g/kg/ml/l) for `cost` ₪.
        Updates the item's last purchase (and so its unit cost)."""
        item = self.get_object()
        d = request.data
        try:
            qty = Decimal(str(d.get("quantity")))
            cost = Decimal(str(d.get("cost") or "0"))
        except Exception:
            raise ValidationError({"quantity": "أدخل كمية وسعراً صحيحين."})
        unit = d.get("unit") or item.purchase_unit
        if qty <= 0 or cost < 0:
            raise ValidationError({"quantity": "أدخل كمية وسعراً صحيحين."})
        if models.InventoryItem.base_unit_of(unit) != item.unit:
            raise ValidationError({"unit": "وحدة لا تناسب هذا الصنف."})
        cu = (d.get("client_uuid") or "").strip() or None
        if cu:
            found = models.StockMove.objects.for_pharmacy(self.store_id).filter(client_uuid=cu).first()
            if found:
                return Response(_move_json(found, True))
        with transaction.atomic():
            item = models.InventoryItem.objects.for_pharmacy(self.store_id).select_for_update().get(pk=item.pk)
            item.purchase_qty = qty
            item.purchase_unit = unit
            item.purchase_cost = cost
            if d.get("expiry_date"):
                item.expiry_date = finance._parse(d.get("expiry_date"))
            if d.get("supplier"):
                item.supplier = str(d.get("supplier"))[:255]
            item.save()
            move = _apply_move(
                item, models.StockMove.Kind.PURCHASE,
                models.InventoryItem.to_base(qty, unit), request.user,
                note=d.get("note") or "", client_uuid=cu, total_cost=cost,
            )
        return Response(_move_json(move, True), status=201)

    @action(detail=True, methods=["post"])
    def waste(self, request, pk=None):
        item = self.get_object()
        d = request.data
        try:
            qty = Decimal(str(d.get("quantity")))
        except Exception:
            raise ValidationError({"quantity": "أدخل كمية صحيحة."})
        unit = d.get("unit") or item.unit
        if qty <= 0:
            raise ValidationError({"quantity": "أدخل كمية صحيحة."})
        if models.InventoryItem.base_unit_of(unit) != item.unit:
            raise ValidationError({"unit": "وحدة لا تناسب هذا الصنف."})
        cu = (d.get("client_uuid") or "").strip() or None
        if cu:
            found = models.StockMove.objects.for_pharmacy(self.store_id).filter(client_uuid=cu).first()
            if found:
                return Response(_move_json(found, _is_owner(request.user)))
        with transaction.atomic():
            item = models.InventoryItem.objects.for_pharmacy(self.store_id).select_for_update().get(pk=item.pk)
            move = _apply_move(
                item, models.StockMove.Kind.WASTE,
                -models.InventoryItem.to_base(qty, unit), request.user,
                reason=(d.get("reason") or "")[:120], note=d.get("note") or "", client_uuid=cu,
            )
        return Response(_move_json(move, _is_owner(request.user)), status=201)

    @action(detail=False, methods=["post"])
    def stocktake(self, request):
        """A جرد: `counts: [{item, counted}]` in BASE units. One move per item
        whose count differs; items not listed are left alone."""
        counts = request.data.get("counts") or []
        cu = (request.data.get("client_uuid") or "").strip()
        note = (request.data.get("note") or "")[:255]
        done = []
        with transaction.atomic():
            for i, row in enumerate(counts):
                try:
                    counted = Decimal(str(row.get("counted")))
                except Exception:
                    continue
                if counted < 0:
                    continue
                item = (
                    models.InventoryItem.objects.for_pharmacy(self.store_id)
                    .select_for_update().filter(pk=row.get("item")).first()
                )
                if item is None:
                    continue
                key = f"{cu}:{item.pk}" if cu else None
                if key and models.StockMove.objects.for_pharmacy(self.store_id).filter(client_uuid=key).exists():
                    continue
                diff = counted - Decimal(item.stock or 0)
                if diff == 0:
                    continue
                m = _apply_move(item, models.StockMove.Kind.COUNT, diff, request.user,
                                note=note, client_uuid=key, reason="جرد")
                done.append(m.pk)
        return Response({"moves": len(done)})

    @action(detail=False, methods=["get"])
    def insights(self, request):
        """GET /inventory-items/insights/?period=… — the overview tab."""
        from apps.store import breakdowns

        r = finance.resolve_range(request.query_params)
        return Response(breakdowns.inventory_insights(self.store_id, r["start"], r["end"]))

    @action(detail=False, methods=["get"])
    def summary(self, request):
        owner = _is_owner(request.user)
        qs = models.InventoryItem.objects.for_pharmacy(self.store_id).filter(is_active=True)
        items = list(qs)
        today = finance.today()
        month_start = today.replace(day=1)
        lo, hi = finance.bounds(month_start, today)
        moves = models.StockMove.objects.for_pharmacy(self.store_id).filter(created_at__gte=lo, created_at__lt=hi)
        out = {
            "items": len(items),
            "low": sum(1 for i in items if i.reorder_level and 0 < Decimal(i.stock) <= Decimal(i.reorder_level)),
            "out": sum(1 for i in items if Decimal(i.stock) <= 0),
            "expiring": sum(1 for i in items if i.expiry_date and (i.expiry_date - today).days <= 7),
            "categories": sorted({i.category for i in items if i.category}),
        }
        if owner:
            out["stock_value"] = str(sum((i.stock_value for i in items), Decimal("0.00")))
            out["purchases_month"] = str(moves.filter(kind="purchase").aggregate(n=Sum("total_cost"))["n"] or Decimal("0.00"))
            out["waste_month"] = str(moves.filter(kind="waste").aggregate(n=Sum("total_cost"))["n"] or Decimal("0.00"))
        return Response(out)


def _apply_move(item, kind, qty, user, *, reason="", note="", client_uuid=None, total_cost=None):
    """Write one StockMove and move the item's stock by `qty` (base units)."""
    qty = Decimal(qty)
    unit_cost = Decimal(item.unit_cost or 0)
    if total_cost is None:
        total_cost = (abs(qty) * unit_cost).quantize(CENT)
    item.stock = Decimal(item.stock or 0) + qty
    item.save(update_fields=["stock", "updated_at"])
    return models.StockMove.objects.create(
        store_id=item.store_id, item=item, kind=kind, quantity=qty,
        unit_cost=unit_cost, total_cost=total_cost, stock_after=item.stock,
        reason=reason or "", note=(note or "")[:255],
        created_by=user if getattr(user, "is_authenticated", False) else None,
        client_uuid=client_uuid,
    )


def _move_json(m, owner: bool) -> dict:
    out = {
        "id": m.pk, "item": m.item_id, "kind": m.kind, "kind_label": m.get_kind_display(),
        "quantity": str(m.quantity), "stock_after": str(m.stock_after),
        "reason": m.reason, "note": m.note,
        "sale": m.sale_id, "receipt_code": m.receipt_code, "product_name": m.product_name,
        "created_by_name": m.created_by.staff_name if m.created_by_id else "",
        "created_at": m.created_at.isoformat(),
    }
    if owner:
        out["unit_cost"] = str(m.unit_cost)
        out["total_cost"] = str(m.total_cost)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Shifts
# ═══════════════════════════════════════════════════════════════════════════
class ShiftSerializer(OwnerOnlyFieldsMixin, serializers.ModelSerializer):
    owner_only_fields = ("wage_per_day",)
    hours = serializers.DecimalField(max_digits=5, decimal_places=2, read_only=True)
    crosses_midnight = serializers.BooleanField(read_only=True)

    class Meta:
        model = models.Shift
        fields = ["id", "name", "start", "end", "wage_per_day", "position", "is_active",
                  "hours", "crosses_midnight"]


class ShiftViewSet(OwnerWritesMixin, StoreScopedMixin, viewsets.ModelViewSet):
    queryset = models.Shift.objects.unscoped()
    serializer_class = ShiftSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def perform_create(self, serializer):
        serializer.save(store_id=self.store_id)
        invalidate_reports_cache(self.store_id)


# ═══════════════════════════════════════════════════════════════════════════
# Expenses
# ═══════════════════════════════════════════════════════════════════════════
class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ExpenseCategory
        fields = ["id", "name", "key", "position"]
        read_only_fields = ["key"]


class ExpenseCategoryViewSet(OwnerOnlyMixin, StoreScopedMixin, viewsets.ModelViewSet):
    queryset = models.ExpenseCategory.objects.unscoped()
    serializer_class = ExpenseCategorySerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def list(self, request, *args, **kwargs):
        finance.ensure_default_categories(self.store_id)
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(store_id=self.store_id)

    def perform_destroy(self, instance):
        if instance.expenses.exists() or instance.recurring.exists():
            raise ValidationError({"detail": "التصنيف مستخدم في مصاريف مسجلة — انقلها أولاً."})
        instance.delete()


class _CategoryField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        request = self.context.get("request")
        return models.ExpenseCategory.objects.for_pharmacy(request_pharmacy_id(request))


class _StaffField(serializers.PrimaryKeyRelatedField):
    """A person on THIS shop's staff — never another shop's, never a
    platform superuser."""

    def get_queryset(self):
        from apps.accounts.models import User

        request = self.context.get("request")
        return User.objects.filter(store_id=request_pharmacy_id(request), is_superuser=False)


class _ExpenseKindMixin(serializers.Serializer):
    """What every expense form shares: who it was for, and the rule that a
    salary always names its person."""

    category_key = serializers.CharField(source="category.key", read_only=True)
    staff = _StaffField(required=False, allow_null=True)
    staff_name = serializers.SerializerMethodField()

    def get_staff_name(self, obj) -> str:
        return obj.staff.staff_name if getattr(obj, "staff_id", None) else ""

    def validate(self, attrs):
        attrs = super().validate(attrs)
        cat = attrs.get("category", getattr(self.instance, "category", None))
        staff = attrs.get("staff", getattr(self.instance, "staff", None))
        if cat is not None and cat.key == "salaries" and staff is None:
            raise serializers.ValidationError({"staff": "اختر الموظف صاحب الراتب."})
        if cat is not None and cat.key != "salaries" and "staff" in attrs:
            attrs["staff"] = None
        return attrs


class ExpenseSerializer(_ExpenseKindMixin, serializers.ModelSerializer):
    category = _CategoryField()
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = models.Expense
        fields = ["id", "category", "category_name", "category_key", "amount", "period", "paid_on",
                  "note", "staff", "staff_name", "payee", "client_uuid", "created_at"]
        read_only_fields = ["id", "created_at"]


class ExpenseViewSet(OwnerOnlyMixin, StoreScopedMixin, viewsets.ModelViewSet):
    queryset = models.Expense.objects.unscoped().select_related("category")
    serializer_class = ExpenseSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleEnabled]
    required_module = "pnl"
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        month = self.request.query_params.get("month")
        if month:
            d = finance._parse(f"{month}-01" if len(month) == 7 else month)
            if d:
                qs = qs.filter(period=d.replace(day=1))
        return qs

    def perform_create(self, serializer):
        cu = (serializer.validated_data.get("client_uuid") or "").strip() or None
        if cu:
            found = models.Expense.objects.for_pharmacy(self.store_id).filter(client_uuid=cu).first()
            if found:
                serializer.instance = found
                return
        serializer.save(store_id=self.store_id, created_by=self.request.user)
        invalidate_reports_cache(self.store_id)

    @action(detail=False, methods=["get"])
    def month(self, request):
        """Everything that counts against one month: the one-offs, the
        recurring ones, and the total by category."""
        m = request.query_params.get("month") or finance.today().strftime("%Y-%m")
        first = finance._parse(f"{m}-01") or finance.today().replace(day=1)
        first = first.replace(day=1)
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        finance.ensure_default_categories(self.store_id)
        one_off = models.Expense.objects.for_pharmacy(self.store_id).filter(period=first).select_related("category", "staff")
        recurring = [
            r for r in models.RecurringExpense.objects.for_pharmacy(self.store_id).select_related("category", "staff")
            if r.active_in(first)
        ]
        by_cat = finance.opex(self.store_id, first, last)
        prev_last = first - timedelta(days=1)
        prev_first = prev_last.replace(day=1)
        prev = finance.opex(self.store_id, prev_first, prev_last)
        # The last six months, oldest first, for the trend chart.
        trend = []
        m0 = first
        for _ in range(6):
            end = m0.replace(day=calendar.monthrange(m0.year, m0.month)[1])
            trend.append({"month": m0.strftime("%Y-%m"), "total": str(finance.opex(self.store_id, m0, end)["total"])})
            m0 = (m0 - timedelta(days=1)).replace(day=1)
        trend.reverse()
        return Response({
            "trend": trend,
            "month": first.strftime("%Y-%m"),
            "expenses": ExpenseSerializer(one_off, many=True, context={"request": request}).data,
            "recurring": RecurringExpenseSerializer(recurring, many=True, context={"request": request}).data,
            "by_category": by_cat["rows"],
            "total": str(by_cat["total"]),
            "previous_total": str(prev["total"]),
        })


class RecurringExpenseSerializer(_ExpenseKindMixin, serializers.ModelSerializer):
    category = _CategoryField()
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = models.RecurringExpense
        fields = ["id", "category", "category_name", "category_key", "name", "amount", "staff", "staff_name",
                  "payee", "start_month", "end_month"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        s = attrs.get("start_month", getattr(self.instance, "start_month", None))
        e = attrs.get("end_month", getattr(self.instance, "end_month", None))
        if s and e and e.replace(day=1) < s.replace(day=1):
            raise serializers.ValidationError({"end_month": "شهر النهاية قبل شهر البداية."})
        return attrs


class RecurringExpenseViewSet(OwnerOnlyMixin, StoreScopedMixin, viewsets.ModelViewSet):
    queryset = models.RecurringExpense.objects.unscoped().select_related("category")
    serializer_class = RecurringExpenseSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleEnabled]
    required_module = "pnl"
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def perform_create(self, serializer):
        serializer.save(store_id=self.store_id)
        invalidate_reports_cache(self.store_id)


# ═══════════════════════════════════════════════════════════════════════════
# Points bands
# ═══════════════════════════════════════════════════════════════════════════
class EarnRulesView(APIView):
    """GET the ladder (anyone — the till previews points with it).
    PUT replaces the whole ladder (owner): `{rules: [{min_total, max_total,
    rate_percent}]}`. Bands must not overlap; an empty list means the flat
    default rate."""

    permission_classes = [permissions.IsAuthenticated, StoreResolved]

    def get_permissions(self):
        perms = super().get_permissions()
        if self.request.method not in permissions.SAFE_METHODS:
            perms.append(OwnerRequired())
        return perms

    def _payload(self, store_id):
        rules = models.EarnRule.objects.for_pharmacy(store_id).order_by("min_total")
        return {
            "rules": [
                {
                    "id": r.pk, "min_total": str(r.min_total),
                    "max_total": str(r.max_total) if r.max_total is not None else None,
                    "rate_percent": str(r.rate_percent),
                }
                for r in rules
            ],
            "default_rate_percent": str((points_service.EARN_RATE * 100).quantize(CENT)),
            "points_per_ils": points_service.POINTS_PER_ILS,
        }

    def get(self, request):
        return Response(self._payload(request_pharmacy_id(request)))

    def put(self, request):
        store_id = request_pharmacy_id(request)
        raw = request.data.get("rules")
        if not isinstance(raw, list):
            raise ValidationError({"rules": "أرسل قائمة الشرائح."})
        rows = []
        for i, r in enumerate(raw):
            try:
                lo = Decimal(str(r.get("min_total") or "0"))
                hi = r.get("max_total")
                hi = None if hi in (None, "") else Decimal(str(hi))
                rate = Decimal(str(r.get("rate_percent")))
            except Exception:
                raise ValidationError({"rules": f"الشريحة {i + 1}: أرقام غير صحيحة."})
            if lo < 0 or rate < 0 or rate > 100 or (hi is not None and hi <= lo):
                raise ValidationError({"rules": f"الشريحة {i + 1}: الحدود أو النسبة غير منطقية."})
            rows.append((lo, hi, rate))
        rows.sort(key=lambda t: t[0])
        for a, b in zip(rows, rows[1:]):
            if a[1] is None or a[1] > b[0]:
                raise ValidationError({"rules": "الشرائح متداخلة — كل شريحة تبدأ حيث تنتهي التي قبلها."})
        with transaction.atomic():
            models.EarnRule.objects.for_pharmacy(store_id).delete()
            for i, (lo, hi, rate) in enumerate(rows):
                models.EarnRule.objects.create(
                    store_id=store_id, min_total=lo, max_total=hi, rate_percent=rate, position=i
                )
        return Response(self._payload(store_id))


class PointsPreviewView(APIView):
    """What a bill of `amount` would earn — so the till can show it before
    the sale is rung, with the same arithmetic the server will use."""

    permission_classes = [permissions.IsAuthenticated, StoreResolved]

    def get(self, request):
        store_id = request_pharmacy_id(request)
        try:
            amount = Decimal(str(request.query_params.get("amount") or "0"))
        except Exception:
            amount = Decimal("0")
        rate = points_service.rate_for(store_id, amount)
        return Response({
            "amount": str(amount),
            "rate_percent": str((rate * 100).quantize(CENT)),
            "points": points_service.points_for(amount, rate=rate),
        })


# ═══════════════════════════════════════════════════════════════════════════
# P&L
# ═══════════════════════════════════════════════════════════════════════════
class ReportsPnlView(ReportsBaseView):
    """GET /reports/pnl/?period=day|week|month|custom&date=&start=&end=&shift=

    Owner only, behind the `pnl` module. Not cached: an expense typed a
    second ago must show in the statement the owner is looking at.
    """

    required_module = "pnl"

    def get(self, request):
        rng = finance.resolve_range(request.query_params)
        shift = None
        sid = request.query_params.get("shift")
        if sid:
            shift = models.Shift.objects.for_pharmacy(self.store_id).filter(pk=sid).first()
        data = finance.pnl(
            self.store_id, rng["start"], rng["end"], shift=shift, period=rng["period"]
        )
        data["shifts"] = [
            {"id": s.pk, "name": s.name, "start": s.start.strftime("%H:%M"), "end": s.end.strftime("%H:%M")}
            for s in models.Shift.objects.for_pharmacy(self.store_id).filter(is_active=True)
        ]
        return Response(data)


class ReportsHoursView(ReportsBaseView):
    """GET /reports/hours/?period=…  hour × category grid."""

    required_module = "reports"

    def get(self, request):
        rng = finance.resolve_range(request.query_params)
        shift = None
        sid = request.query_params.get("shift")
        if sid:
            shift = models.Shift.objects.for_pharmacy(self.store_id).filter(pk=sid).first()
        data = finance.hourly_by_category(self.store_id, rng["start"], rng["end"], shift)
        data["range"] = {"start": rng["start"].isoformat(), "end": rng["end"].isoformat(), "period": rng["period"]}
        return Response(data)


# ═══════════════════════════════════════════════════════════════════════════
# Report tabs — one question each (apps/store/breakdowns.py)
# ═══════════════════════════════════════════════════════════════════════════
class _RangeReport(ReportsBaseView):
    required_module = "reports"

    def range(self):
        return finance.resolve_range(self.request.query_params)


class ReportsItemsView(_RangeReport):
    """GET /reports/items/?period=… every menu item, ranked."""

    def get(self, request):
        from apps.store import breakdowns

        r = self.range()
        data = breakdowns.items_report(self.store_id, r["start"], r["end"], period=r["period"])
        data["range"] = {"start": r["start"].isoformat(), "end": r["end"].isoformat(), "period": r["period"]}
        return Response(data)


class ReportsItemDetailView(_RangeReport):
    """GET /reports/items/<id>/?period=… one drink."""

    def get(self, request, pk):
        from apps.store import breakdowns

        r = self.range()
        data = breakdowns.item_detail(self.store_id, pk, r["start"], r["end"])
        if data is None:
            return Response({"detail": "غير موجود."}, status=404)
        return Response(data)


class ReportsTimesView(_RangeReport):
    def get(self, request):
        from apps.store import breakdowns

        r = self.range()
        return Response(breakdowns.times_report(self.store_id, r["start"], r["end"]))


class ReportsShiftsView(_RangeReport):
    required_module = "pnl"

    def get(self, request):
        from apps.store import breakdowns

        r = self.range()
        return Response(breakdowns.shifts_report(self.store_id, r["start"], r["end"]))


class ReportsCustomersView(_RangeReport):
    def get(self, request):
        from apps.store import breakdowns

        r = self.range()
        return Response(breakdowns.customers_report(self.store_id, r["start"], r["end"]))


class ReportsReturnsView(_RangeReport):
    def get(self, request):
        from apps.store import breakdowns

        r = self.range()
        return Response(breakdowns.returns_report(self.store_id, r["start"], r["end"]))


# ═══════════════════════════════════════════════════════════════════════════
# Inventory categories (the dropdown on an item)
# ═══════════════════════════════════════════════════════════════════════════
DEFAULT_INV_CATEGORIES = ["قهوة", "ألبان", "سيرب", "فواكه", "جاف", "تغليف", "تنظيف"]


class InventoryCategorySerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()

    class Meta:
        model = models.InventoryCategory
        fields = ["id", "name", "position", "items"]

    def get_items(self, obj) -> int:
        return models.InventoryItem.objects.for_pharmacy(obj.store_id).filter(category=obj.name).count()

    def validate_name(self, v):
        v = (v or "").strip()
        if not v:
            raise serializers.ValidationError("أدخل اسم التصنيف.")
        return v[:80]


class InventoryCategoryViewSet(OwnerWritesMixin, StoreScopedMixin, viewsets.ModelViewSet):
    """The category list behind the dropdown. Seeded on first read from the
    categories items already use, plus a short default list. Renaming one
    renames it on every item; deleting one in use is refused."""

    queryset = models.InventoryCategory.objects.unscoped()
    serializer_class = InventoryCategorySerializer
    permission_classes = [permissions.IsAuthenticated, ModuleEnabled]
    required_module = "stock"
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def list(self, request, *args, **kwargs):
        sid = self.store_id
        qs = models.InventoryCategory.objects.for_pharmacy(sid)
        if not qs.exists():
            used = list(
                models.InventoryItem.objects.for_pharmacy(sid).exclude(category="")
                .values_list("category", flat=True).distinct()
            )
            names = list(dict.fromkeys([*used, *DEFAULT_INV_CATEGORIES]))
            for i, n in enumerate(names):
                models.InventoryCategory.objects.get_or_create(store_id=sid, name=n, defaults={"position": i})
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        name = serializer.validated_data["name"]
        found = models.InventoryCategory.objects.for_pharmacy(self.store_id).filter(name=name).first()
        if found:
            serializer.instance = found
            return
        serializer.save(store_id=self.store_id)

    def perform_update(self, serializer):
        old = serializer.instance.name
        cat = serializer.save()
        if cat.name != old:
            models.InventoryItem.objects.for_pharmacy(self.store_id).filter(category=old).update(category=cat.name)

    def perform_destroy(self, instance):
        if models.InventoryItem.objects.for_pharmacy(self.store_id).filter(category=instance.name).exists():
            raise ValidationError({"detail": "التصنيف مستخدم لأصناف — انقلها لتصنيف آخر أولاً."})
        instance.delete()


# ═══════════════════════════════════════════════════════════════════════════
# Recipes (owner)
# ═══════════════════════════════════════════════════════════════════════════
class ProductRecipeView(APIView):
    """GET /products/<id>/recipe/ — every version of the drink's recipe.
    PUT  {variant: null|id, lines: [{item, quantity, unit}]} — replaces ONE
    version. `quantity` is in `unit` (piece, g, kg, ml, l) and stored in the
    ingredient's base unit; an empty `lines` on a size = use the drink's."""

    permission_classes = [permissions.IsAuthenticated, OwnerRequired, StoreResolved]

    def _product(self, request, pk):
        p = models.Product.objects.for_pharmacy(request_pharmacy_id(request)).filter(pk=pk).first()
        if p is None:
            raise ValidationError({"detail": "غير موجود."})
        return p

    def get(self, request, pk):
        from apps.store import recipes

        p = self._product(request, pk)
        return Response(recipes.recipe_payload(p.store_id, p))

    def _rows(self, p, raw, who=""):
        """Validate one version's lines → [(item, base_qty, unit)]. Nothing is
        written until every version in the request has passed."""
        if not isinstance(raw, list):
            raise ValidationError({"lines": "أرسل قائمة المكونات."})
        pre = f"{who}: " if who else ""
        items = {
            i.pk: i
            for i in models.InventoryItem.objects.for_pharmacy(p.store_id).filter(
                pk__in=[r.get("item") for r in raw if isinstance(r, dict)]
            )
        }
        rows = []
        for n, r in enumerate(raw):
            item = items.get(r.get("item")) if isinstance(r, dict) else None
            if item is None:
                raise ValidationError({"lines": f"{pre}المكوّن {n + 1}: اختر صنفاً من المخزون."})
            unit = r.get("unit") or item.unit
            if models.InventoryItem.base_unit_of(unit) != item.unit:
                raise ValidationError({"lines": f"{pre}{item.name}: وحدة لا تناسب الصنف."})
            try:
                qty = Decimal(str(r.get("quantity")))
            except Exception:
                raise ValidationError({"lines": f"{pre}{item.name}: كمية غير صحيحة."})
            base = models.InventoryItem.to_base(qty, unit).quantize(Decimal("0.001"))
            if base <= 0:
                raise ValidationError({"lines": f"{pre}{item.name}: الكمية يجب أن تكون أكبر من صفر."})
            rows.append((item, base, unit))
        if len({r[0].pk for r in rows}) != len(rows):
            raise ValidationError({"lines": f"{pre}صنف مكرر — اجمع الكمية في سطر واحد."})
        return rows

    def put(self, request, pk):
        """Either one version ({variant, lines}) or several at once
        ({versions: [{variant, lines}, …]}) — the drink form saves the drink
        and every size together, all or nothing."""
        from apps.store import recipes

        p = self._product(request, pk)
        if "versions" in request.data:
            versions = request.data.get("versions")
            if not isinstance(versions, list):
                raise ValidationError({"versions": "أرسل قائمة."})
        else:
            versions = [{"variant": request.data.get("variant"), "lines": request.data.get("lines") or []}]
        labels = dict(p.variants.values_list("pk", "label"))
        plan, seen = [], set()
        for v in versions:
            variant_id = (v or {}).get("variant") or None
            if variant_id and variant_id not in labels:
                raise ValidationError({"variant": "هذا الحجم ليس لهذا المشروب."})
            if variant_id in seen:
                raise ValidationError({"variant": "نفس الحجم مرتين."})
            seen.add(variant_id)
            who = labels.get(variant_id, "") if variant_id else ""
            plan.append((variant_id, self._rows(p, (v or {}).get("lines") or [], who)))
        with transaction.atomic():
            for variant_id, rows in plan:
                models.RecipeLine.objects.for_pharmacy(p.store_id).filter(product=p, variant_id=variant_id).delete()
                for i, (item, base, unit) in enumerate(rows):
                    models.RecipeLine.objects.create(
                        store_id=p.store_id, product=p, variant_id=variant_id, item=item,
                        quantity=base, display_unit=unit, position=i,
                    )
        from apps.store.views import invalidate_pos_catalog_cache

        invalidate_pos_catalog_cache(p.store_id)
        return Response(recipes.recipe_payload(p.store_id, p))

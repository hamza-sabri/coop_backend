from decimal import Decimal

from rest_framework import serializers

from apps.core.permissions import StoreRequired
from apps.core.serializers import ImageUploadMixin
from . import models

_UNSET = object()


class SaleDebtMirrorError(serializers.ValidationError):
    """A credit sale finished without its mirrored Debt.

    Should be impossible. It is raised INSIDE the checkout transaction so the
    whole sale rolls back rather than landing in the ledger with a customer
    balance that silently under-reports what they owe. A 400 (not a 500) is
    deliberate: the offline queue drops the payload and shows the cashier the
    error instead of retrying a permanently-broken sale forever.
    """

    def __init__(self, sale_pk=None, customer_id=None):
        # Logged with detail for Sentry; the cashier sees plain Arabic.
        self.debug_detail = f"sale={sale_pk} customer={customer_id}"
        super().__init__(
            {"payment_method": "تعذّر تسجيل الدين لهذا البيع. أعد المحاولة."}
        )


def _pharmacy_id(serializer) -> int:
    """The requesting user's store — the ONLY source of tenant identity.

    Raises when missing so no write can ever happen without a tenant. Same
    400 "store_id is required" contract as the StoreResolved guard
    (normally unreachable through the API — the permission rejects first).
    """
    request = serializer.context.get("request")
    pid = getattr(getattr(request, "user", None), "store_id", None)
    if not pid:
        raise StoreRequired()
    return pid


def _same_pharmacy_or_die(serializer, obj, label):
    if obj is not None and obj.store_id != _pharmacy_id(serializer):
        raise serializers.ValidationError({label: "غير موجود."})


class CreatableNameField(serializers.Field):
    """FK exposed as its plain name string; unknown names are auto-created.

    Reads as `"مسكنات"` (or `""` when unset); writing a new name creates the
    canonical row on the fly — search-or-create dropdowns need one call only.
    """

    def __init__(self, model, **kwargs):
        self.model = model
        kwargs.setdefault("required", False)
        kwargs.setdefault("allow_null", True)
        super().__init__(**kwargs)

    def to_representation(self, value):
        return value.name if value else ""

    def to_internal_value(self, data):
        name = " ".join(str(data or "").split())
        if not name:
            return None
        pid = _pharmacy_id(self.parent if self.parent else self)
        existing = self.model.objects.for_pharmacy(pid).filter(
            name__iexact=name
        ).first()
        return existing or self.model.objects.create(store_id=pid, name=name)


class ProductVariantSerializer(serializers.ModelSerializer):
    """A sellable sub-SKU (color / size / flavor) with its own price and stock."""

    # Declared explicitly so it serialises as a STRING like every other money
    # field. Left to DRF's default it came back as the float 24.0 while `price`
    # was "24.00" — two money formats in one object is how rounding bugs start.
    suggested_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )
    is_pack = serializers.BooleanField(read_only=True)

    class Meta:
        model = models.ProductVariant
        fields = [
            "id",
            "product",
            "label",
            "pack_size",
            "suggested_price",
            "is_pack",
            "attributes",
            "barcode",
            "price",
            "cost",
            "stock",
            "image",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            # Derived: piece price × pack_size. The DEFAULT a box should cost;
            # `price` is what it actually sells for and may differ.
            "suggested_price",
            "is_pack",
        ]

    def validate_product(self, value):
        _same_pharmacy_or_die(self, value, "product")
        return value

    def validate(self, attrs):
        """Fill the box price in when nobody set one.

        The owner's expectation, in his words: a box defaults to the piece
        price times how many are in it. Shamel carried a per-unit PURCHASE
        price but never a per-unit SELL price, so this multiplication is how
        the box price was derived at import too — the difference is that it is
        now a default rather than the only possibility.
        """
        pack = attrs.get("pack_size", getattr(self.instance, "pack_size", None))
        price = attrs.get("price", getattr(self.instance, "price", None))
        product = attrs.get("product", getattr(self.instance, "product", None))
        if pack and pack > 1 and product is not None and not price:
            attrs["price"] = (
                Decimal(product.price or 0) * Decimal(pack)
            ).quantize(Decimal("0.01"))
        return attrs


class ProductSerializer(ImageUploadMixin, serializers.ModelSerializer):
    """Full CRUD for a med.

    Images: `image`/`image_file` is the MAIN photo (URL or upload, as before).
    `images` reads the ordered secondary gallery as [{id, url}]; upload more
    via `image_files` (repeat the key for several files) and delete existing
    ones via `remove_images` ("3,7" — ids). Max 8 secondary photos.
    `category` / `manufacturer` read and write as plain name strings.
    """

    MAX_GALLERY = 8

    # Declared lax (required=False) so PATCH may omit it; validate() still
    # rejects a CREATE without a name — nothing is ever prefilled from other
    # stores' data.
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    catalog_item = serializers.PrimaryKeyRelatedField(read_only=True)
    product_name = serializers.SerializerMethodField()
    image_file = serializers.ImageField(write_only=True, required=False)
    image_upload_fields = {"image_file": ("image", "products")}
    images = serializers.SerializerMethodField()
    # A photo may be an absolute URL or a path served by the app itself
    # (`/koup/menu/hazelnut.webp`). URLField rejects the second kind, which
    # meant every seeded product refused to save with "enter a valid URL" —
    # against a value the API had just handed the form. Relative paths are
    # legitimate here, so the field validates loosely and the model column
    # (a varchar either way) stores both.
    image = serializers.CharField(
        max_length=1000, required=False, allow_blank=True
    )
    image_files = serializers.ListField(
        child=serializers.ImageField(), write_only=True, required=False
    )
    # Gallery photos added by URL (paste-a-bunch-of-links), the sibling of
    # image_files. Each becomes a ProductImage row, exactly like the main
    # image already accepts a plain URL.
    image_urls = serializers.ListField(
        child=serializers.CharField(max_length=1000), write_only=True, required=False
    )
    remove_images = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )
    category = CreatableNameField(models.Category)
    manufacturer = CreatableNameField(models.Manufacturer)
    variants = ProductVariantSerializer(many=True, read_only=True)
    # Computed expiry tags — derived from expiry_date + the alert window, so
    # they stay correct as days pass with no write. Status is date-based (a
    # product IS expired regardless of stock); the insights COUNTS are the
    # in-stock-only, actionable view. `expiry_alert_default` is injected by the
    # viewset context so we never query the store per row.
    expiry_status = serializers.SerializerMethodField()
    days_to_expiry = serializers.SerializerMethodField()

    def get_days_to_expiry(self, obj) -> int | None:
        from datetime import date

        if not obj.expiry_date:
            return None
        return (obj.expiry_date - date.today()).days

    def get_expiry_status(self, obj) -> str | None:
        from datetime import date

        # In-stock only: an out-of-stock item's expiry is moot until restocked,
        # so it carries no tag (matches the actionable insights counts).
        if not obj.expiry_date or (obj.stock is not None and obj.stock <= 0):
            return None
        days = (obj.expiry_date - date.today()).days
        if days < 0:
            return "expired"
        window = obj.expiry_alert_days or self.context.get("expiry_alert_default", 30)
        return "soon" if days <= window else "ok"

    def get_product_name(self, obj) -> str:
        # Tenant isolation: the shared catalog's "default name" was donated by
        # whichever store listed the barcode first — another tenant's data.
        # The key stays (API shape) but never carries cross-tenant values.
        return ""

    def validate(self, attrs):
        # A create must carry its own name. (The old shared-catalog prefill
        # leaked another store's product name across tenants.)
        name = " ".join(str(attrs.get("name") or "").split())
        if not name and self.instance is None:
            raise serializers.ValidationError({"name": "أدخل اسم الدواء."})
        if name:
            attrs["name"] = name
        elif "name" in attrs:
            attrs.pop("name")  # PATCH with blank name → keep the current one
        return attrs

    def get_images(self, obj) -> list:
        from apps.core.uploads import resolve_stored_url

        return [
            {"id": im.id, "url": resolve_stored_url(im.image)}
            for im in obj.images.all()
        ]

    def _apply_gallery(self, instance, image_files, image_urls, remove_images):
        from apps.core.uploads import store_upload

        if remove_images:
            ids = [
                int(part)
                for part in str(remove_images).split(",")
                if part.strip().isdigit()
            ]
            # Only THIS med's images — ids from other rows/tenants are inert.
            instance.images.filter(id__in=ids).delete()
        files = image_files or []
        urls = [u.strip() for u in (image_urls or []) if str(u).strip()]
        total_new = len(files) + len(urls)
        if total_new:
            existing = instance.images.count()
            if existing + total_new > self.MAX_GALLERY:
                raise serializers.ValidationError(
                    {"image_files": f"الحد الأقصى {self.MAX_GALLERY} صور إضافية."}
                )
            request = self.context.get("request")
            base = (
                instance.images.order_by("-position").values_list("position", flat=True).first()
                or 0
            )
            position = int(base)
            for f in files:
                position += 1
                instance.images.create(
                    image=store_upload(f, "products", request),
                    position=position,
                )
            for u in urls:
                position += 1
                instance.images.create(image=u, position=position)

    def create(self, validated_data):
        cu = validated_data.get("client_uuid")
        if cu:
            existing = models.Product.objects.for_pharmacy(
                _pharmacy_id(self)
            ).filter(client_uuid=cu).first()
            if existing is not None:
                return existing
        image_files = validated_data.pop("image_files", None)
        image_urls = validated_data.pop("image_urls", None)
        remove_images = validated_data.pop("remove_images", "")
        instance = super().create(validated_data)
        self._apply_gallery(instance, image_files, image_urls, remove_images)
        return instance

    def update(self, instance, validated_data):
        image_files = validated_data.pop("image_files", None)
        image_urls = validated_data.pop("image_urls", None)
        remove_images = validated_data.pop("remove_images", "")
        instance = super().update(instance, validated_data)
        self._apply_gallery(instance, image_files, image_urls, remove_images)
        return instance

    def to_representation(self, instance):
        # DRF short-circuits None before to_representation — keep the old
        # string contract ("" when unset) instead of leaking null.
        data = super().to_representation(instance)
        data["category"] = data["category"] or ""
        data["manufacturer"] = data["manufacturer"] or ""
        # No shared-catalog image fallback: a store without its own photo
        # sees none — never another tenant's donated picture.
        return data

    class Meta:
        model = models.Product
        fields = [
            "id",
            "source_id",
            "name",
            "barcode",
            "original_number",
            "alt_barcodes",
            "price",
            "cost",
            "brand",
            "manufacturer",
            "category",
            "stock",
            "expiry_date",
            "expiry_alert_days",
            "expiry_status",
            "days_to_expiry",
            "notes",
            "image",
            "video_url",
            "attributes",
            "additional_metadata",
            "duplicated_products",
            "image_file",
            "images",
            "image_files",
            "image_urls",
            "remove_images",
            "catalog_item",
            "product_name",
            "variants",
            "client_uuid",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            # Maintained by the price-list importer, not edited through the API.
            "additional_metadata",
            "duplicated_products",
            # Derived from expiry_date + the alert window on every read.
            "expiry_status",
            "days_to_expiry",
        ]

    def validate_alt_barcodes(self, value):
        """Extra scannable codes for the SAME product.

        Real shops end up with several codes on one item: the shelf label, the
        supplier's box, a re-printed sticker, the unit code on a multipack. All
        of them must resolve to this product — and none of them may resolve to
        a DIFFERENT one, or a scan becomes a coin toss.

        So: normalise, drop blanks and duplicates, drop the product's own
        primary barcode (it already resolves), and refuse any code another
        product in this store has already claimed.
        """
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("يجب أن تكون قائمة باركودات.")

        codes, seen = [], set()
        for raw in value:
            code = str(raw or "").strip()
            if not code:
                continue
            if len(code) > 120:
                raise serializers.ValidationError(f"باركود طويل جداً: {code[:20]}…")
            if code in seen:
                continue
            seen.add(code)
            codes.append(code)

        primary = str(self.initial_data.get("barcode") or "").strip()
        if not primary and self.instance is not None:
            primary = (self.instance.barcode or "").strip()
        codes = [c for c in codes if c != primary]
        if not codes:
            return []

        pid = _pharmacy_id(self)
        clash = models.Product.objects.for_pharmacy(pid)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        taken = clash.filter(barcode__in=codes).values_list("barcode", "name")
        if taken:
            code, name = taken[0]
            raise serializers.ValidationError(
                f"الباركود {code} مستخدم أصلاً للصنف «{name}»."
            )
        # And against other products' ALT lists, one code at a time — a JSON
        # column can't be joined on.
        for code in codes:
            other = (
                clash.filter(alt_barcodes__icontains=f'"{code}"')
                .values_list("name", flat=True)
                .first()
            )
            if other:
                raise serializers.ValidationError(
                    f"الباركود {code} مستخدم أصلاً للصنف «{other}»."
                )
        return codes


class CustomerSerializer(ImageUploadMixin, serializers.ModelSerializer):
    """Customer profile (managed by staff; customers never log in).

    Set `avatar` to a URL directly, OR upload via `avatar_file`. `outstanding`
    is the sum of `discounted_total` across the customer's unpaid debts.
    """

    avatar_file = serializers.ImageField(write_only=True, required=False)
    image_upload_fields = {"avatar_file": ("avatar", "avatars")}
    outstanding = serializers.SerializerMethodField()
    # Loyalty, read-only: the admin's customers list shows who signed up in the
    # app and where they stand, without a second request per row.
    beans = serializers.IntegerField(source="loyalty.beans", read_only=True, default=0)
    tier = serializers.CharField(source="loyalty.tier", read_only=True, default="single")
    signed_up = serializers.SerializerMethodField()
    # Declared explicitly (instead of the auto unique validator) so blanks map to
    # NULL and uniqueness only applies to real numbers.
    phone = serializers.CharField(
        max_length=40, required=False, allow_blank=True, allow_null=True
    )

    class Meta:
        model = models.Customer
        fields = [
            "id",
            "name",
            "phone",
            "gender",
            "avatar",
            "avatar_file",
            "clerk_id",
            "email",
            "beans",
            "tier",
            "signed_up",
            "notes",
            "status",
            "outstanding",
            "client_uuid",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        cu = validated_data.get("client_uuid")
        if cu:
            existing = models.Customer.objects.for_pharmacy(
                _pharmacy_id(self)
            ).filter(client_uuid=cu).first()
            if existing is not None:
                return existing
        return super().create(validated_data)

    def validate_phone(self, value):
        # Empty phone -> NULL, so many customers may have no phone at all.
        return (value or "").strip() or None

    def validate(self, attrs):
        phone = attrs.get("phone")
        if phone:
            # Unique per store — other tenants may share the number.
            qs = models.Customer.objects.for_pharmacy(_pharmacy_id(self)).filter(
                phone=phone
            )
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"phone": "رقم الهاتف مستخدم لزبون آخر."}
                )
        return attrs

    def get_outstanding(self, obj) -> str:
        # Uses the viewset's annotation when present; otherwise computes directly
        # (e.g. on the object returned right after a create).
        value = getattr(obj, "outstanding", None)
        if value is None:
            from django.db.models import Sum

            value = obj.debts.filter(is_paid=False).aggregate(
                s=Sum("discounted_total")
            )["s"] or Decimal("0.00")
        return f"{Decimal(value):.2f}"

    def get_signed_up(self, obj) -> bool:
        """Did they come in through the app, or add at the counter?"""
        return bool(obj.clerk_id)

class DebtItemSerializer(serializers.ModelSerializer):
    """A single med line inside a debt. `line_total` is computed server-side."""

    # unscoped() lookup base: validate() below rejects any product/variant
    # outside the requester's store (_same_pharmacy_or_die), so a foreign
    # id can resolve but never pass validation.
    product = serializers.PrimaryKeyRelatedField(
        queryset=models.Product.objects.unscoped(),
        required=False,
        allow_null=True,
    )
    variant = serializers.PrimaryKeyRelatedField(
        queryset=models.ProductVariant.objects.unscoped(),
        required=False,
        allow_null=True,
    )
    # Declared explicitly (no default) so we can tell "omitted" from "sent as 0".
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )

    class Meta:
        model = models.DebtItem
        fields = [
            "id",
            "product",
            "variant",
            "medication_name",
            "variant_label",
            "unit_price",
            "quantity",
            "line_total",
        ]
        read_only_fields = ["id", "variant_label", "line_total"]

    def validate(self, attrs):
        med = attrs.get("product")
        variant = attrs.get("variant")
        _same_pharmacy_or_die(self, med, "product")
        # The variant must be checked too (same rule as SaleItemSerializer):
        # a cross-tenant variant id must never resolve — its label/price
        # would be snapshotted into this tenant's debt.
        if variant is not None and variant.product.store_id != _pharmacy_id(
            self
        ):
            raise serializers.ValidationError({"variant": "غير موجود."})
        if not med and not attrs.get("medication_name"):
            raise serializers.ValidationError(
                "Each item needs a `product` (id) or a `medication_name`."
            )
        if not med and attrs.get("unit_price") is None:
            raise serializers.ValidationError(
                "`unit_price` is required for a free-text item (no `product`)."
            )
        return attrs


class DebtSerializer(serializers.ModelSerializer):
    """A debt with its meds.

    - `total` is computed from the items at purchase time and is READ-ONLY.
    - `discounted_total` defaults to `total`; send it to override (a discount).
    - Each item snapshots the med's name and price, so later catalogue changes
      never rewrite an existing debt.
    """

    items = DebtItemSerializer(many=True, required=False)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_phone = serializers.CharField(source="customer.phone", read_only=True)
    # Who recorded the debt (display name). `created_by` is set server-side.
    created_by_name = serializers.SerializerMethodField()
    # Declared without a default so an omitted value means "match total".
    discounted_total = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )
    # Optional direct amount for a debt with NO line items ("customer owes 100").
    # When items are provided they win; otherwise `total` is set from `amount`.
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, write_only=True
    )

    class Meta:
        model = models.Debt
        fields = [
            "id",
            "customer",
            "customer_name",
            "customer_phone",
            "items",
            "total",  # editable=False on the model -> read-only here
            "discounted_total",
            "amount",
            "is_paid",
            "note",
            "client_uuid",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def get_revision_count(self, obj) -> int:
        # Annotated on the list queryset so browsing history stays one query;
        # counted directly for a single sale (detail, and the PATCH response,
        # which is the one place the annotation cannot be there).
        annotated = getattr(obj, "revision_count_annotated", None)
        if annotated is not None:
            return annotated
        if not obj.pk:
            return 0
        return models.SaleRevision.unguarded.filter(sale_id=obj.pk).count()

    def get_created_by_name(self, obj) -> str:
        user = getattr(obj, "created_by", None)
        if not user:
            return ""
        full = (user.get_full_name() or "").strip()
        return full or user.get_username()

    def validate(self, attrs):
        # The customer must belong to the requesting user's store.
        _same_pharmacy_or_die(self, attrs.get("customer"), "customer")
        return attrs

    def _snapshot_items(self, items_data):
        """Freeze med name + price onto each line at write time."""
        rows = []
        for item in items_data:
            med = item.get("product")
            variant = item.get("variant")
            name = item.get("medication_name") or (med.name if med else "")
            unit_price = item.get("unit_price")
            if unit_price is None:
                unit_price = (
                    variant.price
                    if variant
                    else (med.price if med else Decimal("0.00"))
                )
            rows.append(
                {
                    "product": med,
                    "variant": variant,
                    "medication_name": name,
                    "variant_label": variant.label if variant else "",
                    "unit_price": unit_price,
                    "quantity": item.get("quantity") or 1,
                }
            )
        return rows

    @staticmethod
    def _money(value):
        return Decimal(value).quantize(Decimal("0.01"))

    def create(self, validated_data):
        cu = validated_data.get("client_uuid")
        if cu:
            existing = models.Debt.objects.for_pharmacy(
                _pharmacy_id(self)
            ).filter(client_uuid=cu).first()
            if existing is not None:
                return existing
        items_data = validated_data.pop("items", [])
        amount = validated_data.pop("amount", _UNSET)
        discounted = validated_data.pop("discounted_total", _UNSET)
        debt = models.Debt.objects.create(**validated_data)
        if items_data:
            for row in self._snapshot_items(items_data):
                models.DebtItem.objects.create(debt=debt, **row)  # save() sets line_total
            debt.recalculate_total(save=False)
        elif amount is not _UNSET:
            # Item-less debt: the total is the amount entered directly.
            debt.total = self._money(amount)
        debt.discounted_total = debt.total if discounted is _UNSET else discounted
        debt.save()
        return debt

    def update(self, instance, validated_data):
        items_data = validated_data.pop("items", _UNSET)
        amount = validated_data.pop("amount", _UNSET)
        discounted = validated_data.pop("discounted_total", _UNSET)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        # Recompute the (read-only) total when items or a direct amount are sent.
        if items_data is not _UNSET:
            instance.items.all().delete()
            if items_data:
                for row in self._snapshot_items(items_data):
                    models.DebtItem.objects.create(debt=instance, **row)
                instance.recalculate_total(save=False)
            elif amount is not _UNSET:
                instance.total = self._money(amount)
            else:
                instance.total = Decimal("0.00")
            if discounted is _UNSET:
                instance.discounted_total = instance.total
        elif amount is not _UNSET:
            # Direct amount without an items key -> becomes an item-less debt.
            instance.items.all().delete()
            instance.total = self._money(amount)
            if discounted is _UNSET:
                instance.discounted_total = instance.total

        if discounted is not _UNSET:
            instance.discounted_total = discounted
        instance.save()
        return instance


class SaleItemSerializer(serializers.ModelSerializer):
    """A single med line inside a sale. `line_total` is computed server-side."""

    # unscoped() lookup base: validate() below rejects any product/variant
    # outside the requester's store (_same_pharmacy_or_die).
    product = serializers.PrimaryKeyRelatedField(
        queryset=models.Product.objects.unscoped(),
        required=False,
        allow_null=True,
    )
    variant = serializers.PrimaryKeyRelatedField(
        queryset=models.ProductVariant.objects.unscoped(),
        required=False,
        allow_null=True,
    )
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=Decimal("0")
    )
    #: Set by the POS when the cashier overrode the price at the till. NULL
    #: means unit_price WAS the catalogue price.
    original_unit_price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=Decimal("0"),
    )
    price_was_overridden = serializers.BooleanField(read_only=True)

    class Meta:
        model = models.SaleItem
        fields = [
            "id",
            "product",
            "variant",
            "medication_name",
            "variant_label",
            # What the customer asked for on this line — writable, unlike the
            # other snapshots, because only the till knows it.
            "note",
            "category",
            "unit_price",
            "original_unit_price",
            "price_was_overridden",
            "quantity",
            "line_total",
        ]
        read_only_fields = [
            "id",
            "variant_label",
            "category",
            "line_total",
            "price_was_overridden",
        ]

    def validate(self, attrs):
        med = attrs.get("product")
        variant = attrs.get("variant")
        _same_pharmacy_or_die(self, med, "product")
        if variant is not None and variant.product.store_id != _pharmacy_id(
            self
        ):
            raise serializers.ValidationError({"variant": "غير موجود."})
        if not med and not attrs.get("medication_name"):
            raise serializers.ValidationError(
                "Each item needs a `product` (id) or a `medication_name`."
            )
        if not med and attrs.get("unit_price") is None:
            raise serializers.ValidationError(
                "`unit_price` is required for a free-text item (no `product`)."
            )
        # An "override" equal to the catalogue price is not an override —
        # store NULL, so reports never have to filter that noise out.
        original = attrs.get("original_unit_price")
        if original is not None and original == attrs.get("unit_price"):
            attrs["original_unit_price"] = None
        return attrs


class SaleSerializer(serializers.ModelSerializer):
    """A POS checkout with its lines.

    - `total` is computed from the items and READ-ONLY; `discounted_total`
      defaults to `total` (send a lower value for a discount).
    - `payment_method="debt"` requires a `customer` and creates a linked Debt
      so the amount appears in the customer's balance.
    - Stock is decremented on create (restored by the viewset on delete).
    """

    items = SaleItemSerializer(many=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_phone = serializers.CharField(source="customer.phone", read_only=True)
    # A face on the sales list. App customers have one; a walk-in falls back to
    # an initial, so the column never goes ragged.
    customer_avatar = serializers.CharField(source="customer.avatar", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    #: How many earlier versions this sale has. 0 means it has never been
    #: edited — the UI marks anything above that so a corrected invoice is
    #: never mistaken for an original.
    revision_count = serializers.SerializerMethodField()
    discounted_total = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )

    class Meta:
        model = models.Sale
        fields = [
            "id",
            "customer",
            "customer_name",
            "customer_phone",
            "customer_avatar",
            "payment_method",
            "is_return",
            "items",
            "total",
            "discounted_total",
            "debt",
            "note",
            "client_uuid",
            "receipt_code",
            "created_by",
            "created_by_name",
            "revision_count",
            "created_at",
            "beans_earned",
            "is_paid",
            "updated_at",
        ]
        read_only_fields = ["id", "debt", "created_by", "created_at", "updated_at"]

    def get_revision_count(self, obj) -> int:
        # Annotated on the list queryset so browsing history stays one query;
        # counted directly for a single sale (detail, and the PATCH response,
        # which is the one place the annotation cannot be there).
        annotated = getattr(obj, "revision_count_annotated", None)
        if annotated is not None:
            return annotated
        if not obj.pk:
            return 0
        return models.SaleRevision.unguarded.filter(sale_id=obj.pk).count()

    def get_created_by_name(self, obj) -> str:
        user = getattr(obj, "created_by", None)
        if not user:
            return ""
        full = (user.get_full_name() or "").strip()
        return full or user.get_username()

    def validate(self, attrs):
        # Customer (when given) must belong to the requesting user's store;
        # each item's product is checked in SaleItemSerializer.validate.
        _same_pharmacy_or_die(self, attrs.get("customer"), "customer")
        if not attrs.get("items"):
            raise serializers.ValidationError({"items": "أضف صنفاً واحداً على الأقل."})

        # On an edit the client may send only what changed, so a field that is
        # absent means "leave it as it is" — read it off the sale, not off the
        # payload. Checking the payload alone would let a credit sale be edited
        # into having no customer, which is a debt with nobody attached to it.
        def eff(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None)

        if eff("payment_method") == "debt" and not eff("customer"):
            raise serializers.ValidationError(
                {"customer": "بيع بالدين يتطلب اختيار الزبون."}
            )
        if eff("is_return") and eff("payment_method") == "debt":
            raise serializers.ValidationError(
                {"payment_method": "الإرجاع يكون نقدياً فقط."}
            )
        return attrs

    def _snapshot_items(self, items_data):
        rows = []
        for item in items_data:
            med = item.get("product")
            variant = item.get("variant")
            name = item.get("medication_name") or (med.name if med else "")
            unit_price = item.get("unit_price")
            if unit_price is None:
                unit_price = (
                    variant.price
                    if variant
                    else (med.price if med else Decimal("0.00"))
                )
            rows.append(
                {
                    "product": med,
                    "variant": variant,
                    "medication_name": name,
                    "variant_label": variant.label if variant else "",
                    "note": (item.get("note") or "").strip()[:255],
                    # Snapshot the category NAME (survives catalogue edits).
                    "category": (
                        med.category.name if med and med.category_id else ""
                    ),
                    "unit_price": unit_price,
                    # NULL unless the cashier overrode the price at the till.
                    # Deliberately NOT mirrored onto the Debt below: a debt is
                    # what the customer owes, and they owe the charged price.
                    "original_unit_price": item.get("original_unit_price"),
                    "quantity": item.get("quantity") or 1,
                }
            )
        return rows

    def _receipt_code(self, store_id, supplied, when=None):
        """Settle on the number that will be printed as the barcode.

        The client's own code is kept whenever it can be — that is the only
        way a receipt printed during an internet cut still finds its sale
        after the sync. It is dropped if it is malformed (the scanner would
        read back something else) or already used in this store (scanning it
        would land on the wrong sale). A generated replacement is tried a few
        times; the DB constraint is the real arbiter, not this check.
        """
        code = (supplied or "").strip()
        if code and models.Sale.RECEIPT_CODE_RE.match(code):
            taken = (
                models.Sale.objects.for_pharmacy(store_id)
                .filter(receipt_code=code)
                .exists()
            )
            if not taken:
                return code
        for _ in range(8):
            candidate = models.Sale.new_receipt_code(when)
            if not (
                models.Sale.objects.for_pharmacy(store_id)
                .filter(receipt_code=candidate)
                .exists()
            ):
                return candidate
        # Eight collisions in a row is not a thing that happens; if it did,
        # blank is honest — the sale is still recorded, it just has no barcode.
        return ""

    def _existing_by_uuid(self, store_id, client_uuid):
        """The already-recorded sale for this idempotency key, if any."""
        if not client_uuid:
            return None
        return (
            models.Sale.objects.for_pharmacy(store_id)
            .filter(client_uuid=client_uuid)
            .prefetch_related("items")
            .first()
        )

    def create(self, validated_data):
        from django.db import IntegrityError, transaction
        from django.db.models import F

        # Offline sync idempotency: a re-sent sale (same client_uuid, same
        # tenant) returns the ORIGINAL instead of creating a duplicate. This is
        # what makes retrying a queued offline checkout safe.
        client_uuid = validated_data.get("client_uuid") or None
        store_id = validated_data.get("store_id")
        existing = self._existing_by_uuid(store_id, client_uuid)
        if existing is not None:
            return existing

        items_data = validated_data.pop("items")
        discounted = validated_data.pop("discounted_total", _UNSET)
        validated_data["receipt_code"] = self._receipt_code(
            store_id, validated_data.get("receipt_code")
        )

        try:
            with transaction.atomic():
                sale = models.Sale.objects.create(**validated_data)
                rows = self._snapshot_items(items_data)
                # Returns put stock back; sales take it out.
                delta = 1 if sale.is_return else -1
                for row in rows:
                    models.SaleItem.objects.create(sale=sale, **row)
                    if row["variant"] is not None:
                        models.ProductVariant.objects.for_pharmacy(
                            store_id
                        ).filter(pk=row["variant"].pk).update(
                            stock=F("stock") + delta * row["quantity"]
                        )
                    elif row["product"] is not None:
                        models.Product.objects.for_pharmacy(
                            store_id
                        ).filter(pk=row["product"].pk).update(
                            stock=F("stock") + delta * row["quantity"]
                        )
                sale.recalculate_total(save=False)
                sale.discounted_total = (
                    sale.total if discounted is _UNSET else Decimal(discounted)
                )

                # Credit sale → mirror into a Debt so balances stay correct.
                # Keyed on customer_id (the raw column), never the related
                # object: the FK descriptor can involve a fresh query, and a
                # credit sale silently losing its debt is money going missing.
                if sale.payment_method == "debt" and sale.customer_id:
                    debt = models.Debt.objects.create(
                        store_id=sale.store_id,
                        customer_id=sale.customer_id,
                        created_by=validated_data.get("created_by"),
                        note=f"بيع رقم {sale.pk}",
                    )
                    for row in rows:
                        models.DebtItem.objects.create(
                            debt=debt,
                            product=row["product"],
                            variant=row["variant"],
                            medication_name=row["medication_name"],
                            variant_label=row["variant_label"],
                            unit_price=row["unit_price"],
                            quantity=row["quantity"],
                        )
                    debt.recalculate_total(save=False)
                    debt.discounted_total = sale.discounted_total
                    debt.save()
                    sale.debt = debt

                sale.save()

                # Money guard: a credit sale WITHOUT its debt is a wrong
                # customer balance — worse than a failed checkout, because
                # nobody notices. Fail the whole transaction instead (the
                # cashier sees an error and retries; the client_uuid makes
                # that retry safe) so a bad row can never reach the ledger.
                if sale.payment_method == "debt" and not sale.debt_id:
                    raise SaleDebtMirrorError(sale.pk, sale.customer_id)
        except IntegrityError:
            # Two concurrent requests raced on the same client_uuid — the loser
            # returns the winner's sale rather than erroring.
            existing = self._existing_by_uuid(store_id, client_uuid)
            if existing is not None:
                return existing
            raise
        return sale

    # ── editing an existing sale ─────────────────────────────────────────
    #
    # The cashier rang the wrong item, or the wrong quantity, and the customer
    # is still at the counter. Re-ringing means the receipt already handed over
    # points at a voided invoice and the day's history shows two sales for one
    # basket. So a sale is corrected IN PLACE: same id, same receipt code, same
    # position in the day — and the complete previous state is kept in
    # SaleRevision, because in-place editing is also exactly how a till gets
    # robbed.

    #: Never rewritten by an edit, whatever the client sends.
    #:
    #: `receipt_code` is printed on paper the customer is holding — changing it
    #: orphans that receipt. `created_at` is when the money changed hands, not
    #: when somebody fixed a typo; letting an edit move it would let a sale be
    #: quietly walked into another day's takings. `client_uuid` is the offline
    #: idempotency key and belongs to the original checkout.
    _IMMUTABLE_ON_EDIT = ("receipt_code", "client_uuid", "created_at", "created_by")

    @staticmethod
    def _stock_deltas(rows, *, is_return):
        """Per-product/variant stock movement for one set of lines.

        Returns ({product_id: delta}, {variant_id: delta}). A sale takes stock
        out, a return puts it back.
        """
        from collections import defaultdict

        products, variants = defaultdict(Decimal), defaultdict(Decimal)
        sign = Decimal("1") if is_return else Decimal("-1")
        for row in rows:
            qty = Decimal(str(row["quantity"] or 0))
            if row.get("variant") is not None:
                variants[row["variant"].pk] += sign * qty
            elif row.get("product") is not None:
                products[row["product"].pk] += sign * qty
        return products, variants

    @staticmethod
    def _existing_deltas(sale):
        """The movement the sale's CURRENT lines already applied to stock."""
        from collections import defaultdict

        products, variants = defaultdict(Decimal), defaultdict(Decimal)
        sign = Decimal("1") if sale.is_return else Decimal("-1")
        for item in sale.items.all():
            qty = Decimal(str(item.quantity or 0))
            if item.variant_id:
                variants[item.variant_id] += sign * qty
            elif item.product_id:
                products[item.product_id] += sign * qty
        return products, variants

    def _rebuild_debt(self, sale, rows, created_by):
        """Bring the linked Debt back in line with the edited sale.

        Kept as the SAME Debt row wherever possible, so anything already
        pointing at it — payments, the customer's history — still does.
        """
        want_debt = sale.payment_method == "debt" and sale.customer_id
        debt = sale.debt if sale.debt_id else None

        if not want_debt:
            # Switched to cash. The debt is unpaid (a paid one is refused
            # before we get here), so it should simply stop existing.
            if debt is not None:
                sale.debt = None
                sale.debt_id = None
                debt.delete()
            return

        if debt is None:
            debt = models.Debt.objects.create(
                store_id=sale.store_id,
                customer_id=sale.customer_id,
                created_by=created_by,
                note=f"بيع رقم {sale.pk}",
            )
            sale.debt = debt
        else:
            # The sale may have been moved to a different customer.
            if debt.customer_id != sale.customer_id:
                debt.customer_id = sale.customer_id
            models.DebtItem.unguarded.filter(debt=debt).delete()

        for row in rows:
            models.DebtItem.objects.create(
                debt=debt,
                product=row["product"],
                variant=row["variant"],
                medication_name=row["medication_name"],
                variant_label=row["variant_label"],
                unit_price=row["unit_price"],
                quantity=row["quantity"],
            )
        debt.recalculate_total(save=False)
        debt.discounted_total = sale.discounted_total
        debt.save()

    @staticmethod
    def _locked_sale(store_id, pk):
        """The sale, row-locked for the duration of the edit.

        Locked for the same reason a void is: two edits arriving together would
        each reverse the ORIGINAL stock movement, and the second would win —
        leaving stock adjusted for a version of the sale that no longer exists.

        NO select_related here, and that is the whole point of this being its
        own method.

        `customer` and `debt` are both nullable, so select_related turns them
        into LEFT OUTER JOINs, and PostgreSQL refuses outright:

            NotSupportedError: FOR UPDATE cannot be applied to the nullable
            side of an outer join

        SQLite has no row locks at all, so Django simply omits the FOR UPDATE
        clause there and the whole test suite passes — the failure only exists
        in production. Both attributes are read a moment later; letting them
        load lazily costs two small queries on an operation that happens a few
        times a day.

        prefetch_related is fine: it runs as a SEPARATE query, not a join.
        """
        return (
            models.Sale.objects.for_pharmacy(store_id)
            .select_for_update()
            .prefetch_related("items")
            .get(pk=pk)
        )

    def update(self, instance, validated_data):
        from django.db import transaction
        from django.db.models import F

        for field in self._IMMUTABLE_ON_EDIT:
            validated_data.pop(field, None)
        validated_data.pop("store_id", None)

        items_data = validated_data.pop("items")
        discounted = validated_data.pop("discounted_total", _UNSET)
        store_id = instance.store_id
        editor = None
        request = self.context.get("request")
        if request is not None and getattr(request, "user", None) is not None:
            if request.user.is_authenticated:
                editor = request.user

        with transaction.atomic():
            sale = self._locked_sale(store_id, instance.pk)

            # A settled debt has already been counted as money received. Moving
            # the sale under it would silently change what the customer paid,
            # weeks after they paid it — refuse and let a human decide.
            if sale.debt_id and sale.debt and sale.debt.is_paid:
                raise serializers.ValidationError(
                    {
                        "detail": "لا يمكن تعديل بيع بالدين تم سداده — "
                        "ألغِ البيع وسجّله من جديد."
                    }
                )

            # 1. Keep the version being replaced, whole, before anything moves.
            version = models.SaleRevision.unguarded.filter(sale=sale).count() + 1
            models.SaleRevision.objects.create(
                sale=sale,
                version=version,
                edited_by=editor,
                snapshot=models.SaleRevision.snapshot_of(sale),
            )

            # 2. Work out the NET stock movement in one pass, so a line whose
            #    quantity went 3 → 4 costs one unit, not a give-back-3-take-4
            #    round trip that briefly invents stock and loses the difference
            #    if anything fails between the two.
            rows = self._snapshot_items(items_data)
            is_return = validated_data.get("is_return", sale.is_return)
            if sale.moves_stock():
                old_p, old_v = self._existing_deltas(sale)
                new_p, new_v = self._stock_deltas(rows, is_return=is_return)
                for ids, old, new, model in (
                    ("product", old_p, new_p, models.Product),
                    ("variant", old_v, new_v, models.ProductVariant),
                ):
                    for pk in set(old) | set(new):
                        delta = new.get(pk, Decimal("0")) - old.get(pk, Decimal("0"))
                        if delta:
                            model.objects.for_pharmacy(store_id).filter(
                                pk=pk
                            ).update(stock=F("stock") + delta)

            # 3. Replace the lines.
            models.SaleItem.unguarded.filter(sale=sale).delete()
            for field, value in validated_data.items():
                setattr(sale, field, value)
            created = [
                models.SaleItem.objects.create(sale=sale, **row) for row in rows
            ]

            # Summed from the rows just written, NOT via recalculate_total():
            # `sale` arrived with its old items prefetched, so `self.items.all()`
            # would still hand back the lines this edit just deleted and the
            # total would be the one we are replacing.
            sale.total = sum(
                (i.line_total for i in created), Decimal("0.00")
            ).quantize(Decimal("0.01"))
            sale.discounted_total = (
                sale.total if discounted is _UNSET else Decimal(discounted)
            )

            # 4. The customer's balance has to follow the sale.
            self._rebuild_debt(sale, rows, sale.created_by)
            sale.save()

            if sale.payment_method == "debt" and not sale.debt_id:
                raise SaleDebtMirrorError(sale.pk, sale.customer_id)

            # Read it back before it is serialised. `sale` was loaded with its
            # items prefetched and this method deleted every one of them, so
            # the object still carries the OLD lines in its prefetch cache —
            # the response would show the cashier the basket they just
            # corrected, which reads exactly like the edit did not save.
            return (
                models.Sale.objects.for_pharmacy(store_id)
                .select_related("customer", "created_by", "debt")
                .prefetch_related("items")
                .get(pk=sale.pk)
            )

    # ── loyalty + settlement, read-only, for the customer's order list ──────
    beans_earned = serializers.SerializerMethodField()
    is_paid = serializers.SerializerMethodField()

    def get_beans_earned(self, obj) -> int:
        """Beans this order actually put in the cup, from the ledger.

        Read from the ledger rather than recomputed from the total: the rate
        and the tier multiplier both change over time, and a receipt from
        March must keep saying what it said in March.
        """
        rows = getattr(obj, "bean_rows", None)
        if rows is None:
            return 0
        return sum(r.delta for r in rows.all() if r.delta > 0)

    def get_is_paid(self, obj) -> bool:
        """A sale is settled unless it is a debt that has not been paid."""
        debt = getattr(obj, "debt", None)
        return True if debt is None else bool(getattr(debt, "is_paid", False))

class TaxonomySerializerBase(serializers.ModelSerializer):
    """Name + how many meds use it (fed to the searchable dropdowns)."""

    count = serializers.IntegerField(read_only=True, default=0)


class CategorySerializer(TaxonomySerializerBase):
    class Meta:
        model = models.Category
        fields = ["id", "name", "count", "icon"]


class ManufacturerSerializer(TaxonomySerializerBase):
    class Meta:
        model = models.Manufacturer
        fields = ["id", "name", "count"]


class PurchaseItemSerializer(serializers.ModelSerializer):
    # A plain int (not a related field) so the parent serializer can scope it to
    # the store instead of trusting a global PK.
    product_id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = models.PurchaseItem
        fields = [
            "id",
            "product_id",
            "medication_name",
            "barcode",
            "quantity",
            "unit_cost",
            "line_total",
        ]
        read_only_fields = ["id", "line_total"]


class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseItemSerializer(many=True)
    created_by_name = serializers.CharField(
        source="created_by.username", read_only=True, default=""
    )

    class Meta:
        model = models.PurchaseOrder
        fields = [
            "id",
            "supplier",
            "status",
            "note",
            "total_cost",
            "received_at",
            "created_by_name",
            "created_at",
            "items",
        ]
        read_only_fields = [
            "id",
            "status",
            "total_cost",
            "received_at",
            "created_by_name",
            "created_at",
        ]

    def _write_items(self, order, items, pid):
        # Only LINK products that belong to THIS store — a foreign id is
        # dropped to null (the line still records the snapshot name/qty/cost).
        wanted = [it.get("product_id") for it in items if it.get("product_id")]
        valid = set(
            models.Product.objects.for_pharmacy(pid)
            .filter(id__in=wanted)
            .values_list("id", flat=True)
        )
        for it in items:
            mid = it.get("product_id")
            order.items.create(
                product_id=mid if mid in valid else None,
                medication_name=it.get("medication_name", ""),
                barcode=it.get("barcode", ""),
                quantity=it.get("quantity", Decimal("1")),
                unit_cost=it.get("unit_cost", Decimal("0")),
            )

    def create(self, validated_data):
        items = validated_data.pop("items", [])
        pid = validated_data.get("store_id")
        order = models.PurchaseOrder(**validated_data)
        order.save()
        self._write_items(order, items, pid)
        order.recalculate_total()
        return order

    def update(self, instance, validated_data):
        if instance.status == "received":
            raise serializers.ValidationError("لا يمكن تعديل طلبية مستلمة.")
        items = validated_data.pop("items", None)
        validated_data.pop("store_id", None)  # never reassign tenant
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if items is not None:
            instance.items.all().delete()
            self._write_items(instance, items, instance.store_id)
        instance.recalculate_total()
        return instance


# <scaffold:serializers>

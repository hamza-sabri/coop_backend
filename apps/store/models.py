import re
import secrets
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from .managers import TenantManager

# Run `python manage.py setup_model store <ModelName>` after editing this file.

TWO_PLACES = Decimal("0.01")


class Plan(TimeStampedModel):
    """A subscription tier — a named bundle of feature modules.

    Managed from the Django admin: create plans (أساسي / متقدم / شامل ...),
    tick the modules each one includes, and assign a plan per store. A
    store's effective modules = its plan's modules ∪ any extra modules in
    `Store.enabled_modules` (à-la-carte add-ons). Pharmacies with NO plan
    keep the legacy behavior: empty enabled_modules = everything.
    """

    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    # Informational — shown in admin/marketing, not billed automatically.
    price_monthly = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    # Module keys from apps.store.modules.MODULES.
    modules = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Plan"
        verbose_name_plural = "Plans"

    def __str__(self):
        return self.name


class Store(TimeStampedModel):
    """A tenant. EVERY piece of data belongs to exactly one store.

    Staff users carry a store FK and every API query/write is scoped to it
    server-side — tenants can never see or touch each other's data. `slug` is
    the stable key used by per-client domains and the public price page.
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=64, unique=True)
    # A store's OWN domain, when it has one (e.g. "saydaliyat-x.ps").
    # Normally a tenant is reached at "<slug>.<root domain>" and the frontend
    # reads the slug straight off the subdomain. A custom domain carries no
    # slug, so the host has to be looked up here instead. NULL (not "") when
    # unset, so any number of stores can have no custom domain while the
    # ones that do stay unique.
    host = models.CharField(
        max_length=253,
        null=True,
        blank=True,
        default=None,
        unique=True,
        db_index=True,
        help_text="نطاق خاص بالصيدلية (اختياري) — مثال: saydaliyat-x.ps",
    )
    phone = models.CharField(max_length=40, blank=True)
    address = models.CharField(max_length=255, blank=True)
    # External URL or a `b2://<key>` storage marker (signed on read) — a plain
    # CharField because URL validation would reject the b2:// markers.
    logo = models.CharField(max_length=1000, blank=True)
    is_active = models.BooleanField(default=True)
    # Subscription tier. With a plan: modules = plan.modules ∪ enabled_modules
    # (extras). Without: legacy behavior (empty enabled_modules = everything).
    plan = models.ForeignKey(
        Plan, related_name="stores", null=True, blank=True, on_delete=models.SET_NULL
    )
    # Feature modules this tenant subscribes to (keys from
    # apps.store.modules.MODULES). EMPTY LIST = ALL MODULES, so legacy
    # tenants keep full access without a backfill. Enforced by the
    # `ModuleEnabled` permission on every module-owned endpoint. When a plan
    # is set this list becomes the tenant's à-la-carte EXTRAS on top of it.
    enabled_modules = models.JSONField(default=list, blank=True)
    # Store-wide default for the expiry alert window: a product is flagged
    # "near expiry" when it's within this many days of its expiry date. A
    # product may override it per-row (Product.expiry_alert_days).
    expiry_alert_days = models.PositiveSmallIntegerField(default=30)
    #: The till's quick-tap groups — the handful of things this shop sells
    #: constantly, one tap from the counter.
    #:
    #: A list of {"key", "label", "icon", "kind", "product_ids", "amounts"}.
    #: `kind` is "products" (tap the circle, pick an item) or "amounts" (tap
    #: it, pick a value — phone credit, gift cards, deposits, anything sold at
    #: fixed prices under one name).
    #:
    #: Stored on the STORE, not in the browser: this is how the shop works,
    #: not a preference of one machine. A cleared cache, or a second till, must
    #: not lose it.
    #:
    #: Empty by default. Every shop builds its own — a default list would be a
    #: guess about a trade we know nothing about.
    pos_quick_groups = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Store"
        verbose_name_plural = "Pharmacies"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Hosts are case-insensitive and must never be stored as "" — an empty
        # string would collide under the unique constraint the moment a second
        # store had no custom domain.
        self.host = (self.host or "").strip().lower().rstrip(".") or None
        return super().save(*args, **kwargs)

    @classmethod
    def resolve_host(cls, host, root_domain):
        """The store serving this Host header, or None.

        Order matters: an explicit custom domain wins, then the
        "<slug>.<root>" convention. Anything else returns None — we never
        guess, because guessing wrong serves one store another store's
        data.
        """
        if not host:
            return None
        h = str(host).strip().lower().split(":")[0].rstrip(".")
        if not h:
            return None

        exact = cls.objects.filter(host=h, is_active=True).first()
        if exact is not None:
            return exact

        root = str(root_domain or "").strip().lower().rstrip(".")
        if root and h.endswith(f".{root}"):
            sub = h[: -(len(root) + 1)]
            # Only a single label is a tenant: "a.b.root" is not "a".
            if sub and "." not in sub:
                return cls.objects.filter(slug=sub, is_active=True).first()
        return None


class Category(TimeStampedModel):
    """A product category (تصنيف) — one canonical row per name & store."""

    store = models.ForeignKey(
        Store, related_name="categories", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=120)
    # Which glyph the POS shows on this category's filter circle. A key,
    # not a class name — the frontend owns the icon set.
    icon = models.CharField(max_length=32, blank=True)

    objects = TenantManager()  # reads require .for_pharmacy() / .unscoped()
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["name"]
        unique_together = [("store", "name")]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        verbose_name = "Category"
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


class Manufacturer(TimeStampedModel):
    """A producing company (الشركة المنتجة) — one row per name & store."""

    store = models.ForeignKey(
        Store, related_name="manufacturers", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=255)

    objects = TenantManager()  # reads require .for_pharmacy() / .unscoped()
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["name"]
        unique_together = [("store", "name")]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        verbose_name = "Manufacturer"
        verbose_name_plural = "Manufacturers"

    def __str__(self):
        return self.name


class CatalogItem(TimeStampedModel):
    """SHARED catalog entry — ONE row per barcode across the whole platform.

    Holds only tenant-neutral defaults: the first name and photos we ever
    received for this barcode. Everything store-specific (price, stock,
    their own name/photos) lives on Product, which links here. NEVER add
    prices, stock, or anything that hints at which stores stock it.
    """

    barcode = models.CharField(max_length=120, unique=True)
    name = models.CharField(max_length=255)
    image = models.URLField(max_length=1000, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "CatalogItem (shared)"
        verbose_name_plural = "Products (shared)"

    def __str__(self):
        return f"{self.name} [{self.barcode}]"


class CatalogItemImage(TimeStampedModel):
    """Default gallery photo for a shared product."""

    catalog_item = models.ForeignKey(
        CatalogItem, related_name="images", on_delete=models.CASCADE
    )
    image = models.URLField(max_length=1000)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"Image #{self.pk} of product {self.catalog_item_id}"


class Product(TimeStampedModel):
    """A store's LISTING of a product — the per-store layer.

    Name, price, cost, stock and photos here are this store's own values.
    When a barcode is set the row links to the shared CatalogItem, whose default
    name/photos act as fallbacks for anything the store hasn't customised.

    `price` is the retail selling price used when the med is added to a debt.
    `image` is stored as a plain URL — the API also accepts an uploaded file on
    `image_file`, which is pushed to storage (Backblaze B2 when configured, else
    local) and the resulting URL saved here.
    """

    store = models.ForeignKey(
        Store, related_name="products", on_delete=models.CASCADE
    )
    # Link into the SHARED catalog (set automatically from the barcode).
    catalog_item = models.ForeignKey(
        CatalogItem,
        related_name="listings",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    # Stable id from the source price-list — used for idempotent re-imports.
    source_id = models.CharField(max_length=64, blank=True, db_index=True)

    name = models.CharField(max_length=255, db_index=True)
    barcode = models.CharField(max_length=120, blank=True, db_index=True)
    # The product's «الرقم الأصلي» (original number) from the price list — a
    # SECOND identity code that may be printed on the box instead of, or beside,
    # the barcode. Kept for display/search and ALSO copied into `alt_barcodes`
    # so scanning it resolves the product. On import, `barcode` and
    # `original_number` are treated as one identity: any code that matches means
    # the SAME product (this is what stops the same item being re-created under a
    # different code, and stops distinct items collapsing by name).
    original_number = models.CharField(max_length=120, blank=True, db_index=True)
    # Extra scannable barcodes for the SAME product (e.g. Hesabate's
    # «باركود الوحدات» unit/packaging codes, and the original_number above).
    # Same stock, same price — just more ways to scan it. list[str] of strings.
    alt_barcodes = models.JSONField(default=list, blank=True)

    # Retail / selling price (what a customer is charged). Snapshotted onto a
    # debt line when the med is sold, so later price changes don't rewrite history.
    price = models.DecimalField(
        "retail price",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    brand = models.CharField(max_length=255, blank=True, db_index=True)
    # Category & manufacturer are canonical rows (searchable dropdowns in the
    # UI); the API still reads/writes them as plain name strings.
    manufacturer = models.ForeignKey(
        Manufacturer,
        related_name="products",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    category = models.ForeignKey(
        Category,
        related_name="products",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    stock = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("0")
    )
    # Restock trigger for the purchase quota: when stock ≤ reorder_level the item
    # is suggested for reorder. 0 = fall back to the global low-stock threshold.
    reorder_level = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("0")
    )
    # Expiry date of the product (the soonest-expiring stock). NULL = no expiry
    # tracked / perpetual item. Drives the "expired" and "near-expiry" tags,
    # which are COMPUTED from this date + the alert window, never stored.
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    # Per-product override for the near-expiry window (days). NULL = fall back
    # to the store default (Store.expiry_alert_days, itself 30 by default).
    expiry_alert_days = models.PositiveSmallIntegerField(null=True, blank=True)

    notes = models.TextField(blank=True)
    image = models.URLField(max_length=1000, blank=True)
    # An optional product VIDEO the shopper can watch on the price page — a
    # direct file URL (mp4/webm) or a YouTube/Vimeo link. Display-only; stored
    # like `image` (a plain URL, or a b2://<key> marker signed on read).
    video_url = models.URLField(max_length=1000, blank=True)
    # Free-form product properties the store chooses to record
    # (e.g. {"اللون": "أحمر", "الحجم": "كبير"}). Optional and display-only.
    attributes = models.JSONField(default=dict, blank=True)
    # Columns the price-list import did NOT recognise are preserved here as
    # {column header: value} so nothing in the uploaded file is ever lost. Set
    # by the importer; display-only, safe to ignore in normal app logic.
    additional_metadata = models.JSONField(default=dict, blank=True)
    # When several rows in one import share this barcode, the most complete /
    # highest-stock row becomes THIS product (the one shown), and every
    # other row is preserved here as a list of raw parsed rows — kept for
    # later use (e.g. promoting them into ProductVariant). Never affects
    # stock, price or sales; purely a stash of the extra rows.
    duplicated_products = models.JSONField(default=list, blank=True)
    # Idempotency key for offline create sync (client-generated UUID). Re-posting
    # the same key returns the original row instead of duplicating it.
    client_uuid = models.CharField(
        max_length=64, null=True, blank=True, default=None, db_index=True
    )

    objects = TenantManager()  # reads require .for_pharmacy() / .unscoped()
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["name"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        verbose_name = "Product"
        verbose_name_plural = "Medications"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "client_uuid"],
                name="uniq_medication_client_uuid",
            )
        ]
        indexes = [
            # Identity is (store, barcode) — every scan/lookup filters on
            # both. Non-unique for now; uniqueness lands in Phase D after the
            # dedup review (see TENANT_ISOLATION_MIGRATION_PLAN.md).
            models.Index(
                fields=["store", "barcode"], name="med_pharmacy_barcode_idx"
            ),
        ]

    def __str__(self):
        return self.name


class ProductVariant(TimeStampedModel):
    """A sellable sub-SKU of a product (color / size / flavor …).

    Each variant carries its OWN barcode, price, cost and stock, so it is
    tracked and sold independently. A product with no variants sells from
    its own stock exactly as before — variants are purely additive.
    """

    product = models.ForeignKey(
        Product, related_name="variants", on_delete=models.CASCADE
    )
    label = models.CharField(max_length=255)
    #: How many BASE units this variant contains — a box of 24, a sleeve of 6.
    #:
    #: A shop that buys by the case needs the number itself, not "عبوة ×24"
    #: buried in a label: without it you can display a box but you cannot price
    #: it, count it, or convert it back to pieces without parsing text.
    #:
    #: NULL/0 = not a pack, just a plain variant (colour, flavour).
    pack_size = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="عدد القطع داخل العبوة — اتركه فارغاً إن لم يكن عبوة",
    )
    attributes = models.JSONField(default=dict, blank=True)
    barcode = models.CharField(max_length=120, blank=True, db_index=True)
    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    stock = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("0")
    )
    image = models.URLField(max_length=1000, blank=True)
    is_active = models.BooleanField(default=True)

    objects = TenantManager("product__store_id")
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["label"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        verbose_name = "Product variant"
        verbose_name_plural = "Product variants"

    @property
    def is_pack(self) -> bool:
        return bool(self.pack_size and self.pack_size > 1)

    def suggested_price(self):
        """What a box SHOULD cost: the piece price times what's inside.

        The default the owner expects, and the number to fall back on when
        nobody has set the box's own price. Shamel carried a per-unit PURCHASE
        price but no per-unit SELL price, so this multiplication is exactly how
        the box price was derived at import — the difference now is that it can
        be overridden and the override is visible.
        """
        if not self.is_pack or self.product_id is None:
            return None
        return (Decimal(self.product.price or 0) * Decimal(self.pack_size)).quantize(
            TWO_PLACES
        )

    def __str__(self):
        return f"{self.product_id} · {self.label}"


class ProductImage(TimeStampedModel):
    """A secondary product photo (the main one lives on Product.image).

    `image` stores either an external URL or a b2://<key> marker that gets
    signed on read — same convention as the main image.
    """

    product = models.ForeignKey(
        Product, related_name="images", on_delete=models.CASCADE
    )
    image = models.URLField(max_length=1000)
    position = models.PositiveIntegerField(default=0)

    objects = TenantManager("product__store_id")
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["position", "id"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        verbose_name = "Product image"
        verbose_name_plural = "Product images"

    def __str__(self):
        return f"Image #{self.pk} of {self.product_id}"


class Customer(TimeStampedModel):
    """A store customer profile.

    Customers are managed by staff and never log in — this is just a record, not
    an auth user. `avatar` is a URL; upload a file to `avatar_file` on the API to
    store it and populate this automatically.
    """

    GENDER_CHOICES = [("male", "Male"), ("female", "Female")]

    store = models.ForeignKey(
        Store, related_name="customers", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=255, db_index=True)
    # Optional, but unique WITHIN the store when set. NULL is stored for
    # "no phone" so any number of customers can have no phone; two stores
    # may each have the same number (it's the same person shopping twice).
    phone = models.CharField(max_length=40, blank=True, null=True, db_index=True)
    gender = models.CharField(
        max_length=10, choices=GENDER_CHOICES, default="male", db_index=True
    )
    avatar = models.URLField(max_length=1000, blank=True)
    # Set when the customer signs into the ordering app. Blank for anyone the
    # staff added at the counter — the two populations share one table because
    # they are one person: the regular who later installs the app must keep
    # their history, not start a second account.
    clerk_id = models.CharField(max_length=64, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    # Free-text status — indexed so it is searchable and filterable.
    status = models.CharField(max_length=255, blank=True, db_index=True)
    client_uuid = models.CharField(
        max_length=64, null=True, blank=True, default=None, db_index=True
    )

    objects = TenantManager()  # reads require .for_pharmacy() / .unscoped()
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["name"]
        unique_together = [("store", "phone")]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        verbose_name = "Customer"
        verbose_name_plural = "Customers"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "client_uuid"],
                name="uniq_customer_client_uuid",
            )
        ]

    def __str__(self):
        return self.name


class Debt(TimeStampedModel):
    """A customer's debt: the meds they bought, plus totals.

    `total` is the frozen sum of the line items at purchase time and CANNOT be
    edited through the API. `discounted_total` starts equal to `total` and may be
    lowered by staff (e.g. a goodwill discount).
    """

    store = models.ForeignKey(
        Store, related_name="debts", on_delete=models.CASCADE
    )
    customer = models.ForeignKey(
        Customer, related_name="debts", on_delete=models.CASCADE
    )
    # Staff member who recorded this debt (kept for audit; survives their deletion).
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    # Frozen — computed from the line items, read-only on the API.
    total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), editable=False
    )
    # Editable; defaults to `total`.
    discounted_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    is_paid = models.BooleanField(default=False)
    note = models.TextField(blank=True)
    client_uuid = models.CharField(
        max_length=64, null=True, blank=True, default=None, db_index=True
    )

    objects = TenantManager()  # reads require .for_pharmacy() / .unscoped()
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        verbose_name = "Debt"
        verbose_name_plural = "Debts"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "client_uuid"],
                name="uniq_debt_client_uuid",
            )
        ]

    def __str__(self):
        return f"Debt #{self.pk} — {self.customer_id}"

    def recalculate_total(self, save=True):
        """Sum the line items into `total`. Returns the new total."""
        total = sum(
            (item.line_total for item in self.items.all()), Decimal("0.00")
        ).quantize(TWO_PLACES)
        self.total = total
        if save:
            self.save(update_fields=["total", "updated_at"])
        return total


class Sale(TimeStampedModel):
    """A completed POS checkout.

    `customer` is optional (walk-in cash sales). When `payment_method` is
    `debt`, a linked `Debt` is created so the amount shows up in the customer's
    balance and every existing debt flow keeps working unchanged.
    """

    PAYMENT_CHOICES = [("cash", "Cash"), ("debt", "Debt")]

    store = models.ForeignKey(
        Store, related_name="sales", on_delete=models.CASCADE
    )
    customer = models.ForeignKey(
        Customer,
        related_name="sales",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    payment_method = models.CharField(
        max_length=10, choices=PAYMENT_CHOICES, default="cash", db_index=True
    )
    # Return (استرجاع): stock goes back UP and the amount counts as negative
    # in every sales statistic.
    is_return = models.BooleanField(default=False, db_index=True)
    # Frozen — computed from the line items, read-only on the API.
    total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), editable=False
    )
    # Editable; defaults to `total` (a discount lowers it).
    discounted_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    # The debt generated for a credit sale (kept for traceability).
    debt = models.ForeignKey(
        Debt, related_name="sales", null=True, blank=True, on_delete=models.SET_NULL
    )
    # Idempotency key for offline POS sync. The client generates one UUID per
    # checkout; re-sending it returns the ORIGINAL sale instead of creating a
    # duplicate — so a sale queued during an internet/power cut can be retried
    # safely without double-charging or double-decrementing stock. NULL for
    # online sales that never needed it (NULLs don't collide in the unique
    # constraint). Scoped per-store, like everything else.
    client_uuid = models.CharField(
        max_length=64, null=True, blank=True, default=None, db_index=True
    )
    #: The number printed as a barcode on the receipt.
    #:
    #: Twelve digits — YYMMDD then six more — so it encodes in Code 128 subset
    #: C (two digits per symbol) and stays narrow enough to scan off a 58mm
    #: roll. The date prefix means the owner can read the day off a receipt
    #: without a scanner.
    #:
    #: The CLIENT generates it, for one reason: a sale rung during an internet
    #: cut prints its receipt before the server has ever heard of it, and a
    #: server-side code would make that printed paper unfindable forever. The
    #: server keeps whatever it is sent unless it is malformed or already
    #: taken, in which case it mints its own.
    #:
    #: Blank is allowed and excluded from the unique constraint, so rows
    #: imported from an older till do not all collide with each other.
    receipt_code = models.CharField(
        max_length=32, blank=True, default="", db_index=True
    )
    note = models.TextField(blank=True)

    objects = TenantManager()  # reads require .for_pharmacy() / .unscoped()
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        verbose_name = "Sale"
        verbose_name_plural = "Sales"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "client_uuid"],
                name="uniq_pharmacy_client_uuid",
            ),
            # Scanning a receipt must land on exactly one sale.
            models.UniqueConstraint(
                fields=["store", "receipt_code"],
                condition=~models.Q(receipt_code=""),
                name="uniq_store_receipt_code",
            ),
        ]

    #: 12 digits: YYMMDD + 6. Anything else is refused rather than printed — a
    #: code the scanner cannot read back is worse than no code.
    RECEIPT_CODE_RE = re.compile(r"^[0-9]{12}$")

    @staticmethod
    def new_receipt_code(when=None) -> str:
        """A fresh receipt number. NOT checked for uniqueness — the caller does
        that against the DB constraint, which is the only check that cannot
        race."""
        when = when or timezone.now()
        return f"{when:%y%m%d}{secrets.randbelow(1_000_000):06d}"

    def __str__(self):
        return f"Sale #{self.pk}"

    def moves_stock(self) -> bool:
        """Whether this sale's quantities are reflected in stock at all.

        False for rows imported from a previous till, which are written
        straight into the table WITHOUT decrementing stock (stock cannot be
        migrated — it has to be counted). Those rows still carry live product
        FKs, so any code that "reverses" them credits back quantities that were
        never taken: void or edit one old 7-unit invoice and a counted shelf of
        10 silently becomes 17, with nothing to say why.

        Every path that touches stock because of a sale — voiding, bulk
        deleting, editing — asks this one question, so the three cannot drift.
        Importers mark such rows by prefixing `note` with the source name and a
        colon (e.g. "legacy:1234").
        """
        note = self.note or ""
        return ":" not in note.split()[0] if note.split() else True

    def recalculate_total(self, save=True):
        total = sum(
            (item.line_total for item in self.items.all()), Decimal("0.00")
        ).quantize(TWO_PLACES)
        self.total = total
        if save:
            self.save(update_fields=["total", "updated_at"])
        return total


class SaleRevision(TimeStampedModel):
    """One past version of a sale, kept whole.

    A sale can be corrected in place — the cashier rang the wrong item, or the
    wrong quantity, and the customer is still standing there. Correcting it
    keeps the receipt number, so the paper already handed over still finds the
    right invoice, and the sale keeps its place in the day's history.

    That is also exactly how a till gets robbed: ring ₪300, take the cash, edit
    the invoice down to ₪30. So nothing is overwritten silently. Every edit
    writes the COMPLETE previous state here first — every line, every price,
    the total, the payment method, who was serving — and the owner can read the
    whole chain back. The snapshot is denormalised JSON on purpose: it has to
    stay readable after the products in it are renamed or deleted, which is the
    moment it matters most.

    `version` numbers the past states: version 1 is the sale as it was
    originally rung, version 2 the state after the first edit, and so on. The
    live row is always the newest state and is not duplicated here.
    """

    sale = models.ForeignKey(
        Sale, related_name="revisions", on_delete=models.CASCADE
    )
    version = models.PositiveIntegerField()
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    #: The sale as it was BEFORE the edit that created this row.
    snapshot = models.JSONField(default=dict)

    objects = TenantManager("sale__store_id")
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["-version"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        constraints = [
            models.UniqueConstraint(
                fields=["sale", "version"], name="uniq_sale_revision_version"
            ),
        ]

    def __str__(self):
        return f"Sale #{self.sale_id} v{self.version}"

    @staticmethod
    def snapshot_of(sale) -> dict:
        """Everything needed to read this version back without the catalogue."""
        return {
            "total": str(sale.total),
            "discounted_total": str(sale.discounted_total),
            "payment_method": sale.payment_method,
            "is_return": sale.is_return,
            "customer_id": sale.customer_id,
            "customer_name": sale.customer.name if sale.customer_id else "",
            "note": sale.note,
            "receipt_code": sale.receipt_code,
            "created_at": sale.created_at.isoformat() if sale.created_at else None,
            "items": [
                {
                    "product_id": i.product_id,
                    "variant_id": i.variant_id,
                    "medication_name": i.medication_name,
                    "variant_label": i.variant_label,
                    "quantity": str(i.quantity),
                    "unit_price": str(i.unit_price),
                    "line_total": str(i.line_total),
                }
                for i in sale.items.all()
            ],
        }


class SaleItem(TimeStampedModel):
    """One med sold within a sale, with name/price/category snapshotted."""

    sale = models.ForeignKey(Sale, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(
        Product,
        related_name="sale_items",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    variant = models.ForeignKey(
        "ProductVariant",
        related_name="sale_items",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    medication_name = models.CharField(max_length=255, blank=True)
    variant_label = models.CharField(max_length=255, blank=True)
    #: What the customer asked for on THIS line — "بدون سكر", "حليب لوز",
    #: "سخن زيادة". Kept per line rather than on the sale: a note that applies
    #: to one drink in an order of four is useless attached to the order.
    #: It is a snapshot, like the name and the label: the sale is a record of
    #: what happened, and what was asked for is part of that.
    note = models.CharField(max_length=255, blank=True)
    # Category snapshot so sales analytics survive catalogue edits.
    category = models.CharField(max_length=120, blank=True, db_index=True)
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    #: The catalogue price at the moment of sale, when the cashier overrode it
    #: at the till (haggling, a damaged tin, a rounded total). NULL means "no
    #: override" — unit_price WAS the catalogue price.
    #:
    #: Without this, an override is invisible after the fact: unit_price alone
    #: cannot distinguish "sold at ₪1 because that is the price" from "sold at
    #: ₪1 because the cashier decided so". The owner needs to see the second
    #: kind, and needs it per line rather than buried in the sale's total.
    original_unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    line_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), editable=False
    )

    objects = TenantManager("sale__store_id")
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["id"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"

    def save(self, *args, **kwargs):
        self.line_total = (
            Decimal(self.unit_price or 0) * (self.quantity or 0)
        ).quantize(TWO_PLACES)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.medication_name} x{self.quantity}"

    @property
    def price_was_overridden(self) -> bool:
        """True when the cashier charged something other than the catalogue
        price. `unit_price` alone cannot tell "sold at ₪1 because that is the
        price" from "sold at ₪1 because the cashier decided so"."""
        return (
            self.original_unit_price is not None
            and self.original_unit_price != self.unit_price
        )

    @property
    def price_override_delta(self) -> Decimal:
        """Signed money given away (negative) or added (positive) on this line."""
        if not self.price_was_overridden:
            return Decimal("0.00")
        return (
            (Decimal(self.unit_price) - Decimal(self.original_unit_price))
            * Decimal(self.quantity or 0)
        ).quantize(TWO_PLACES)

class PurchaseOrder(TimeStampedModel):
    """A restock/purchase order built on the المشتريات page.

    Starts as a DRAFT (no stock effect). Receiving it (status → received) raises
    each line's product stock and refreshes its cost to the purchase cost.
    Deleting a received order reverses the stock. Owner-only, tenant-scoped.
    """

    STATUS_CHOICES = [("draft", "Draft"), ("received", "Received")]

    store = models.ForeignKey(
        Store, related_name="purchase_orders", on_delete=models.CASCADE
    )
    supplier = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default="draft", db_index=True
    )
    note = models.TextField(blank=True)
    # Frozen — recomputed from the line items on save.
    total_cost = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"), editable=False
    )
    received_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()  # reads require .for_pharmacy() / .unscoped()
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        verbose_name = "Purchase order"
        verbose_name_plural = "Purchase orders"

    def __str__(self):
        return f"PO #{self.pk}"

    def recalculate_total(self, save=True):
        total = sum(
            (item.line_total for item in self.items.all()), Decimal("0.00")
        ).quantize(TWO_PLACES)
        self.total_cost = total
        if save:
            self.save(update_fields=["total_cost", "updated_at"])
        return total


class PurchaseItem(TimeStampedModel):
    """One line within a purchase order, med name/barcode snapshotted."""

    order = models.ForeignKey(
        PurchaseOrder, related_name="items", on_delete=models.CASCADE
    )
    product = models.ForeignKey(
        Product,
        related_name="purchase_items",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    medication_name = models.CharField(max_length=255, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    line_total = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00"), editable=False
    )

    objects = TenantManager("order__store_id")
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["id"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"

    def save(self, *args, **kwargs):
        self.line_total = (
            Decimal(self.unit_cost or 0) * (self.quantity or 0)
        ).quantize(TWO_PLACES)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.medication_name} x{self.quantity}"


class PosCartState(TimeStampedModel):
    """The cashier's open POS carts, synced across devices (one row per user)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="pos_cart_state",
        on_delete=models.CASCADE,
    )
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "POS cart state"
        verbose_name_plural = "POS cart states"

    def __str__(self):
        return f"Carts of {self.user_id}"


class DebtItem(TimeStampedModel):
    """One med bought within a debt, with the price snapshotted at purchase time."""

    debt = models.ForeignKey(Debt, related_name="items", on_delete=models.CASCADE)
    # SET_NULL so history survives if a med is later deleted.
    product = models.ForeignKey(
        Product,
        related_name="debt_items",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    variant = models.ForeignKey(
        "ProductVariant",
        related_name="debt_items",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    # Snapshots — preserved even if the med is renamed / repriced / removed.
    medication_name = models.CharField(max_length=255, blank=True)
    variant_label = models.CharField(max_length=255, blank=True)
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    # Computed = unit_price * quantity. Read-only.
    line_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), editable=False
    )

    objects = TenantManager("debt__store_id")
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["id"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"

    def save(self, *args, **kwargs):
        self.line_total = (
            Decimal(self.unit_price or 0) * (self.quantity or 0)
        ).quantize(TWO_PLACES)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.medication_name} x{self.quantity}"


class ScanDaily(TimeStampedModel):
    """Daily aggregate of customer price-check scans (the public /price kiosk).

    One row per (store, day, barcode). Anonymous — no customer identity,
    only what barcode was scanned, how many times that day, and whether it
    matched a priced product. The shopper's browser beacons each scan in the
    background; a Redis counter is bumped and folded into this table once a day
    by `manage.py flush_scan_counters` (a Dokploy cron at 01:00), so the scan
    itself never waits on the database.

    Powers the "تقارير المسح" section of Reports: most-scanned items (demand)
    and barcodes that matched nothing — products customers ask for that the
    store may not stock or hasn't priced.
    """

    store = models.ForeignKey(
        Store, related_name="scan_days", on_delete=models.CASCADE
    )
    day = models.DateField(db_index=True)
    barcode = models.CharField(max_length=120)
    # SET_NULL so history survives if the med is later deleted; the name is
    # snapshotted so the report reads even for unmatched or removed products.
    product = models.ForeignKey(
        Product,
        related_name="scan_days",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    medication_name = models.CharField(max_length=255, blank=True)
    # False = the barcode matched no priced product (a demand signal).
    found = models.BooleanField(default=False)
    count = models.PositiveIntegerField(default=0)

    objects = TenantManager()  # reads require .for_pharmacy() / .unscoped()
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["-day", "-count"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "day", "barcode"],
                name="uniq_scan_pharmacy_day_barcode",
            )
        ]
        indexes = [
            models.Index(fields=["store", "day"], name="scan_pharmacy_day_idx"),
            models.Index(fields=["store", "found"], name="scan_pharmacy_found_idx"),
        ]
        verbose_name = "Scan (daily)"
        verbose_name_plural = "Scans (daily)"

    def __str__(self):
        return f"{self.barcode} ×{self.count} · {self.day}"


class AuditLog(TimeStampedModel):
    """A record of every DESTRUCTIVE bulk action — and the data to undo it.

    Bulk edits/deletes rewrite thousands of rows in one call, which is exactly
    the kind of action a store needs to be able to answer "what happened?"
    and, where possible, "put it back".

    `before` holds the previous values of the affected rows:
        {"<medication_id>": {"price": "0.00", "cost": "3.00"}, ...}
    so `undo` can restore them field-by-field. It is capped (see UNDO_MAX_ROWS)
    — beyond that the action is still logged, just not auto-reversible, and we
    say so honestly instead of pretending.
    """

    #: Above this many rows we log the action but don't keep a full snapshot.
    UNDO_MAX_ROWS = 20_000

    ACTION_BULK_UPDATE = "bulk_update"
    ACTION_BULK_DELETE = "bulk_delete"
    ACTION_DEBT_DELETE = "debt_delete"
    #: A single sale voided. Not "bulk", but the one action a cashier can use
    #: to make money disappear: ring the sale, take the cash, void it. Without
    #: a row here there is nothing to notice, and no way to answer the owner's
    #: "this invoice was here yesterday" — deletion leaves no other trace.
    ACTION_SALE_DELETE = "sale_delete"
    #: A single sale edited in place. Same hole, different shape: editing a
    #: ₪300 invoice down to ₪30 after pocketing the difference leaves the
    #: receipt number, the date and the customer all unchanged. The full
    #: before-state lives in SaleRevision; this row is what makes it show up in
    #: the one list an owner actually reads.
    ACTION_SALE_EDIT = "sale_edit"
    ACTION_CHOICES = [
        (ACTION_BULK_UPDATE, "تعديل جماعي"),
        (ACTION_BULK_DELETE, "حذف جماعي"),
        (ACTION_DEBT_DELETE, "حذف دين"),
        (ACTION_SALE_DELETE, "إلغاء بيع"),
        (ACTION_SALE_EDIT, "تعديل بيع"),
    ]

    store = models.ForeignKey(
        "Store", related_name="audit_logs", on_delete=models.CASCADE
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="audit_logs",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True)
    #: Human summary shown in the UI ("سعر صفر أو بالسالب — 3,534 صنف").
    summary = models.CharField(max_length=255, blank=True)
    #: What was requested (the `changes` payload / filter) — for the "why".
    request = models.JSONField(default=dict, blank=True)
    #: Previous values per row id, for undo. Empty when too large to snapshot.
    before = models.JSONField(default=dict, blank=True)
    affected = models.PositiveIntegerField(default=0)
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="audit_undos",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    objects = TenantManager()  # reads require .for_pharmacy() / .unscoped()
    unguarded = models.Manager()  # Django internals only — never app code

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        indexes = [
            models.Index(fields=["store", "-created_at"], name="audit_pharmacy_time_idx"),
        ]
        verbose_name = "Audit log"
        verbose_name_plural = "Audit log"

    @property
    def can_undo(self) -> bool:
        """Undo is possible while nothing has been undone yet and we kept a
        snapshot. Deletes are logged but not auto-restorable."""
        return (
            self.undone_at is None
            and self.action == self.ACTION_BULK_UPDATE
            and bool(self.before)
        )

    def __str__(self):
        return f"{self.get_action_display()} · {self.affected} · {self.created_at:%Y-%m-%d %H:%M}"


# ═══════════════════════════════════════════════════════════════════════════
#  LOYALTY — «حبّات كوب»
#
#  One currency, one ledger. `BeanLedger` is append-only and is the truth;
#  `LoyaltyProfile.beans` is a cache that can be rebuilt from it at any time.
#  Every write carries an idempotency key, because the two ways this goes
#  wrong in production are a retried request double-crediting someone, and a
#  balance that no longer agrees with its own history.
# ═══════════════════════════════════════════════════════════════════════════


class LoyaltyProfile(models.Model):
    """A customer's standing in the programme. Denormalised for reads."""

    class Tier(models.TextChoices):
        SINGLE = "single", "سنجل"
        DOUBLE = "double", "دوبل"
        TRIPLE = "triple", "تريبل"

    store = models.ForeignKey(
        "store.Store", related_name="loyalty_profiles", on_delete=models.CASCADE
    )
    customer = models.OneToOneField(
        "store.Customer", related_name="loyalty", on_delete=models.CASCADE
    )
    # A CACHE. Rebuildable with: sum(bean_ledger.delta). Never the source.
    beans = models.IntegerField(default=0, db_index=True)
    tier = models.CharField(
        max_length=12, choices=Tier.choices, default=Tier.SINGLE, db_index=True
    )
    # Tiers are held for a window, not earned forever — otherwise the multiplier
    # ratchets up across the whole customer base and never comes back down.
    tier_until = models.DateField(null=True, blank=True)
    visits_this_month = models.PositiveIntegerField(default=0)
    streak_weeks = models.PositiveIntegerField(default=0)
    last_visit_at = models.DateTimeField(null=True, blank=True, db_index=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()
    unguarded = models.Manager()

    class Meta:
        base_manager_name = "unguarded"
        indexes = [models.Index(fields=["store", "tier"])]

    def __str__(self):
        return f"{self.customer_id} · {self.beans}🫘"

    @property
    def multiplier(self):
        return {"single": Decimal("1.00"),
                "double": Decimal("1.25"),
                "triple": Decimal("1.50")}[self.tier]


class BeanLedger(models.Model):
    """Append-only. Every bean that ever moved, and why."""

    class Reason(models.TextChoices):
        EARN = "earn", "شراء"
        BONUS = "bonus", "مكافأة"
        REDEEM = "redeem", "استبدال"
        REFERRAL = "referral", "إحالة"
        SIGNUP = "signup", "تسجيل"
        EXPIRE = "expire", "انتهاء"
        ADJUST = "adjust", "تعديل يدوي"

    store = models.ForeignKey(
        "store.Store", related_name="bean_ledger", on_delete=models.CASCADE
    )
    customer = models.ForeignKey(
        "store.Customer", related_name="bean_ledger", on_delete=models.CASCADE
    )
    delta = models.IntegerField()
    reason = models.CharField(max_length=16, choices=Reason.choices, db_index=True)
    sale = models.ForeignKey(
        "store.Sale", null=True, blank=True, related_name="bean_rows",
        on_delete=models.SET_NULL,
    )
    balance_after = models.IntegerField()
    note = models.CharField(max_length=255, blank=True)
    # Beans expire on INACTIVITY, and only at the base tier. Warned at 30/7/1.
    expires_at = models.DateField(null=True, blank=True, db_index=True)
    # A retried POST must not credit twice. This is the whole defence.
    idempotency_key = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = TenantManager()
    unguarded = models.Manager()

    class Meta:
        base_manager_name = "unguarded"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["store", "customer", "-created_at"])]

    def __str__(self):
        return f"{self.delta:+d} · {self.reason}"


# ---------------------------------------------------------------------------
# Customer-placed orders
#
# A Sale is a COMPLETED till transaction. An Order is the thing that exists
# before that: the customer taps "طلب جديد" in the PWA, the counter sees it,
# makes it, and hands it over. Modelling it as a status on Sale was tempting
# and wrong — a Sale is immutable history that stock and money already moved
# for, while an Order is a short-lived workflow that can be cancelled before
# anything is owed. Keeping them separate means an abandoned order never has
# to be un-rung.
#
# The Sale is created when the order is COLLECTED, and linked back here, so
# reports keep counting money in exactly one place.
# ---------------------------------------------------------------------------
class Order(TimeStampedModel):
    """An order placed by a signed-in customer from the shop app."""

    class Status(models.TextChoices):
        PLACED = "placed", "بانتظار التأكيد"
        ACCEPTED = "accepted", "تم القبول"
        PREPARING = "preparing", "قيد التحضير"
        READY = "ready", "جاهز للاستلام"
        COLLECTED = "collected", "تم الاستلام"
        CANCELLED = "cancelled", "ملغى"

    #: Which statuses may follow which. Enforced in one place so a stray API
    #: call cannot walk an order backwards from collected to preparing, and so
    #: the customer app can grey out impossible buttons from the same table.
    TRANSITIONS: dict[str, tuple[str, ...]] = {
        Status.PLACED: (Status.ACCEPTED, Status.CANCELLED),
        Status.ACCEPTED: (Status.PREPARING, Status.CANCELLED),
        Status.PREPARING: (Status.READY, Status.CANCELLED),
        Status.READY: (Status.COLLECTED, Status.CANCELLED),
        Status.COLLECTED: (),
        Status.CANCELLED: (),
    }

    store = models.ForeignKey(
        Store, related_name="orders", on_delete=models.CASCADE
    )
    #: Required, unlike Sale.customer: a walk-in has no app to order from.
    customer = models.ForeignKey(
        Customer, related_name="orders", on_delete=models.CASCADE
    )
    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.PLACED, db_index=True,
    )
    #: Frozen from the line items when the order is placed. Prices can change
    #: on the menu afterwards; what the customer agreed to must not.
    total = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), editable=False
    )
    note = models.TextField(blank=True)
    #: Set when the order is collected and rung up. One row of money, not two.
    sale = models.ForeignKey(
        "store.Sale", related_name="orders", null=True, blank=True,
        on_delete=models.SET_NULL,
    )
    cancelled_reason = models.CharField(max_length=255, blank=True)
    #: Same idempotency contract as Sale: a phone on a bad connection retries
    #: the POST, and must not end up with two identical orders.
    client_uuid = models.CharField(
        max_length=64, null=True, blank=True, default=None, db_index=True
    )

    objects = TenantManager()
    unguarded = models.Manager()

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        verbose_name = "Order"
        verbose_name_plural = "Orders"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "client_uuid"],
                name="uniq_order_client_uuid",
            ),
        ]
        indexes = [
            # The counter's queue: this store's open orders, oldest first.
            models.Index(fields=["store", "status", "created_at"]),
        ]

    def __str__(self):
        return f"#{self.pk} · {self.get_status_display()}"

    def can_move_to(self, status: str) -> bool:
        return status in self.TRANSITIONS.get(self.status, ())


class OrderItem(models.Model):
    """One line of an order, with the name and price SNAPSHOTTED.

    Same reasoning as SaleItem: a product renamed or repriced next month must
    not rewrite what someone ordered last week, and a deleted product must not
    empty out old orders.
    """

    order = models.ForeignKey(
        Order, related_name="items", on_delete=models.CASCADE
    )
    product = models.ForeignKey(
        Product, related_name="+", null=True, blank=True,
        on_delete=models.SET_NULL,
    )
    variant = models.ForeignKey(
        ProductVariant, related_name="+", null=True, blank=True,
        on_delete=models.SET_NULL,
    )
    name = models.CharField(max_length=255)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("1")
    )
    #: "بدون سكر", "تيك أواي" — per line, as asked for at the counter.
    note = models.CharField(max_length=255, blank=True)

    objects = TenantManager("order__store_id")
    unguarded = models.Manager()

    class Meta:
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"

    def __str__(self):
        return f"{self.name} ×{self.quantity}"


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
class Notification(TimeStampedModel):
    """One thing worth telling a customer about.

    This row is the RECORD; push is only a delivery attempt on top of it. That
    ordering matters: push fails constantly — permission never granted, an iOS
    device that never installed the PWA, an expired subscription, a phone that
    was off — and a notification the customer can still find in the app when
    they next open it is worth far more than one that evaporated.
    """

    class Kind(models.TextChoices):
        ORDER_PLACED = "order_placed", "تم إرسال الطلب"
        ORDER_ACCEPTED = "order_accepted", "تم قبول الطلب"
        ORDER_PREPARING = "order_preparing", "قيد التحضير"
        ORDER_READY = "order_ready", "جاهز للاستلام"
        ORDER_COLLECTED = "order_collected", "تم الاستلام"
        ORDER_CANCELLED = "order_cancelled", "أُلغي الطلب"
        POINTS_EARNED = "points_earned", "نقاط جديدة"
        POINTS_SPENT = "points_spent", "استبدال نقاط"
        POINTS_EXPIRING = "points_expiring", "نقاط على وشك الانتهاء"
        REWARD_UNLOCKED = "reward_unlocked", "مكافأة متاحة"

    store = models.ForeignKey(
        Store, related_name="notifications", on_delete=models.CASCADE
    )
    customer = models.ForeignKey(
        Customer, related_name="notifications", on_delete=models.CASCADE
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    title = models.CharField(max_length=140)
    body = models.CharField(max_length=400, blank=True)
    #: Anything the UI needs to deep-link or render: {"order_id": 12,
    #: "delta": +15}. Deliberately loose — a new notification kind should not
    #: need a migration.
    data = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: When the push was handed to the browser vendor. NULL = never attempted
    #: or no subscription; it says nothing about whether a human saw it.
    pushed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()
    unguarded = models.Manager()

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"
        indexes = [
            # The bell: this customer's unread count, and their feed.
            models.Index(fields=["store", "customer", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} → {self.customer_id}"


class PushSubscription(TimeStampedModel):
    """One browser's Web Push endpoint for one customer.

    One customer can have several — phone, tablet, the shop's iPad — so this
    is keyed on the endpoint, not the customer. Endpoints die silently; a 404
    or 410 from the push service means "gone", and the row is deleted rather
    than retried forever.
    """

    store = models.ForeignKey(
        Store, related_name="push_subscriptions", on_delete=models.CASCADE
    )
    customer = models.ForeignKey(
        Customer, related_name="push_subscriptions", on_delete=models.CASCADE
    )
    #: The URL the push service gave us. Unique across the table — the same
    #: browser re-subscribing must update, not duplicate.
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=255, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()
    unguarded = models.Manager()

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "unguarded"
        default_manager_name = "unguarded"

    def __str__(self):
        return f"{self.customer_id} · {self.endpoint[:40]}…"

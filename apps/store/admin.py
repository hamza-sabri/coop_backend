from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from . import models
from .modules import MODULES


class UnscopedAdminMixin:
    """Platform admin is cross-tenant ops BY DESIGN — the one sanctioned home
    of `.unscoped()`. Tenant models' default managers raise on direct reads
    (see managers.TenantManager), so every admin/inline over a tenant model
    must opt in here, and FK form fields need an unscoped queryset too."""

    def get_queryset(self, request):
        qs = self.model.objects.unscoped()
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        related = db_field.remote_field.model
        manager = getattr(related, "objects", None)
        if "queryset" not in kwargs and hasattr(manager, "unscoped"):
            kwargs["queryset"] = manager.unscoped()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class PharmacyAdminForm(forms.ModelForm):
    """Checkbox picker for the tenant's subscribed modules.

    Leaving ALL boxes unchecked stores [] = every module (legacy default).
    """

    enabled_modules = forms.MultipleChoiceField(
        choices=[(k, f"{v} ({k})") for k, v in MODULES.items()],
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="اتركها كلها فارغة = جميع الوحدات مفعّلة.",
    )

    # Upload straight from the admin — lands on B2 (b2://logos/... marker) via
    # the same store_upload path product photos use. Overwrites `logo`.
    logo_file = forms.ImageField(
        required=False,
        help_text="ارفع شعار الصيدلية (يُخزَّن في B2 ويُوقَّع عند القراءة). يستبدل حقل logo.",
    )

    # The model column is a URLField, but stored values may be `b2://<key>`
    # markers (signed on read) — the URL form validator would reject them on
    # the next save. Plain text field; store_upload fills it.
    logo = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"size": 80, "dir": "ltr"}),
        help_text="رابط خارجي أو مؤشر b2://… (يُملأ تلقائياً عند رفع logo_file).",
    )

    class Meta:
        model = models.Store
        fields = "__all__"

    def clean_logo_file(self):
        """Push the upload to storage during validation so a broken storage
        config (bad B2 key, network) surfaces as a form error, not a 500."""
        upload = self.cleaned_data.get("logo_file")
        if not upload:
            return upload
        from apps.core.uploads import store_upload

        try:
            self._uploaded_logo_url = store_upload(upload, folder="logos")
        except Exception as exc:  # noqa: BLE001 — boto3 raises many types
            raise forms.ValidationError(
                f"فشل رفع الشعار إلى التخزين — تحقق من إعدادات B2 (B2_APPLICATION_KEY). التفاصيل: {exc}"
            ) from exc
        return upload

    def save(self, commit=True):
        from django.core.cache import cache

        instance = super().save(commit=False)
        if getattr(self, "_uploaded_logo_url", ""):
            instance.logo = self._uploaded_logo_url
        if commit:
            instance.save()
            self.save_m2m()
            # Public branding is cached by slug — drop it so a new logo/name
            # shows up without waiting out the TTL.
            cache.delete(f"store:branding:v1:{instance.slug}")
        return instance


class PlanAdminForm(forms.ModelForm):
    """Checkbox picker for the modules bundled in this plan."""

    modules = forms.MultipleChoiceField(
        choices=[(k, f"{v} ({k})") for k, v in MODULES.items()],
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="الوحدات المشمولة في هذه الباقة.",
    )

    class Meta:
        model = models.Plan
        fields = "__all__"


@admin.register(models.Plan)
class PlanAdmin(admin.ModelAdmin):
    form = PlanAdminForm
    list_display = [
        "name",
        "price_monthly",
        "modules_display",
        "pharmacies_count",
        "is_active",
        "sort_order",
    ]
    list_editable = ["sort_order", "is_active"]
    search_fields = ["name"]

    @admin.display(description="Modules")
    def modules_display(self, obj):
        return ", ".join(obj.modules) if obj.modules else "—"

    @admin.display(description="Pharmacies")
    def pharmacies_count(self, obj):
        return obj.stores.count()


@admin.register(models.Store)
class PharmacyAdmin(admin.ModelAdmin):
    form = PharmacyAdminForm
    list_display = [
        "name",
        "slug",
        "plan",
        "phone",
        "is_active",
        "modules_display",
        "created_at",
        "reset_link",
    ]
    list_filter = ["plan", "is_active"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ["name"]}
    readonly_fields = ["reset_link"]

    @admin.display(description="Extra modules")
    def modules_display(self, obj):
        if obj.enabled_modules:
            return ", ".join(obj.enabled_modules)
        return "all (legacy)" if obj.plan_id is None else "—"

    # -- start fresh ------------------------------------------------------
    # A destructive button lives on the object it destroys, not on a global
    # "danger" page: there is then no version of the click that can hit the
    # wrong shop.

    @admin.display(description="تصفير")
    def reset_link(self, obj):
        # The add form renders readonly fields too, and an unsaved store has
        # no pk to reverse against.
        if obj is None or obj.pk is None:
            return "—"
        return format_html(
            '<a class="button" style="background:#b91c1c;color:#fff" href="{}">'
            "امسح التاريخ</a>",
            reverse("admin:store_store_reset", args=[obj.pk]),
        )

    def get_urls(self):
        return [
            path(
                "<int:pk>/reset/",
                self.admin_site.admin_view(self.reset_view),
                name="store_store_reset",
            ),
            *super().get_urls(),
        ]

    def reset_view(self, request, pk):
        from django.conf import settings

        from . import reset as reset_service

        # Superuser only. `admin_view` already requires staff; a shop reset is
        # not a thing an employee with an admin login should be able to do.
        if not request.user.is_superuser:
            raise PermissionDenied

        store = get_object_or_404(models.Store, pk=pk)
        uids = reset_service.firebase_uids(store)
        # Actually parsed, not merely present: a mangled variable would
        # otherwise promise a deletion the confirmation page cannot deliver.
        from apps.store import push as push_service

        ready = bool(
            push_service.credentials_info()
            and (getattr(settings, "FIREBASE_PROJECT_ID", "") or "").strip()
        )
        error = ""

        if request.method == "POST":
            typed = (request.POST.get("confirm") or "").strip()
            if typed != store.slug:
                error = "الاسم المكتوب لا يطابق اسم المتجر. لم يُحذف شيء."
            else:
                done = reset_service.wipe_database(store)
                rows = sum(n for _, n in done)
                messages.success(
                    request,
                    f"تم تصفير «{store.name}»: حُذف {rows} صف. المنيو والموظفون كما هم.",
                )
                if request.POST.get("firebase"):
                    n, note = reset_service.delete_firebase_users(uids)
                    (messages.success if n else messages.warning)(request, note)
                return redirect("admin:store_store_changelist")

        return TemplateResponse(
            request,
            "admin/store/reset.html",
            {
                **self.admin_site.each_context(request),
                "title": f"تصفير المتجر: {store.name}",
                "store": store,
                "counts": reset_service.preview(store),
                "kept": reset_service.KEPT,
                "firebase_count": len(uids),
                "firebase_ready": ready,
                "error": error,
            },
        )


@admin.register(models.Category)
class CategoryAdmin(UnscopedAdminMixin, admin.ModelAdmin):
    list_display = ["name", "store", "created_at"]
    list_filter = ["store"]
    search_fields = ["name"]
    ordering = ["name"]


@admin.register(models.Manufacturer)
class ManufacturerAdmin(UnscopedAdminMixin, admin.ModelAdmin):
    list_display = ["name", "store", "created_at"]
    list_filter = ["store"]
    search_fields = ["name"]
    ordering = ["name"]



class ProductImageInline(admin.TabularInline):
    model = models.CatalogItemImage
    extra = 0
    fields = ["image", "position"]


@admin.register(models.CatalogItem)
class ProductAdmin(admin.ModelAdmin):
    inlines = [ProductImageInline]
    list_display = ["name", "barcode", "created_at"]
    search_fields = ["name", "barcode"]


class MedicationImageInline(UnscopedAdminMixin, admin.TabularInline):
    model = models.ProductImage
    extra = 0
    fields = ["image", "position"]


class MedicationVariantInline(UnscopedAdminMixin, admin.TabularInline):
    model = models.ProductVariant
    extra = 0
    fields = ["label", "barcode", "price", "cost", "stock", "is_active"]


@admin.register(models.Product)
class MedicationAdmin(UnscopedAdminMixin, admin.ModelAdmin):
    inlines = [MedicationImageInline, MedicationVariantInline]
    list_display = ["name", "category", "brand", "price", "cost", "stock", "barcode"]
    search_fields = ["name", "barcode", "brand", "manufacturer__name", "category__name", "source_id"]
    list_filter = ["store", "category", "brand", "created_at"]
    autocomplete_fields = ["category", "manufacturer"]
    ordering = ["name"]


@admin.register(models.ProductVariant)
class MedicationVariantAdmin(UnscopedAdminMixin, admin.ModelAdmin):
    list_display = ["label", "product", "barcode", "price", "cost", "stock", "is_active"]
    search_fields = ["label", "barcode", "medication__name"]
    list_filter = ["is_active", "created_at"]
    ordering = ["label"]


@admin.register(models.Customer)
class CustomerAdmin(UnscopedAdminMixin, admin.ModelAdmin):
    list_display = ["name", "phone", "gender", "status", "created_at"]
    search_fields = ["name", "phone", "status", "notes"]
    list_filter = ["store", "gender", "status", "created_at"]
    ordering = ["name"]


class DebtItemInline(UnscopedAdminMixin, admin.TabularInline):
    model = models.DebtItem
    extra = 0
    fields = ["product", "medication_name", "unit_price", "quantity", "line_total"]
    readonly_fields = ["line_total"]
    autocomplete_fields = ["product"]


@admin.register(models.Debt)
class DebtAdmin(UnscopedAdminMixin, admin.ModelAdmin):
    list_display = ["id", "customer", "total", "discounted_total", "is_paid", "created_at"]
    search_fields = ["customer__name", "customer__phone", "note"]
    list_filter = ["is_paid", "created_at"]
    autocomplete_fields = ["customer"]
    readonly_fields = ["total", "created_at", "updated_at"]
    inlines = [DebtItemInline]

    def save_related(self, request, form, formsets, change):
        """Keep `total` in sync with the inline items after they are saved."""
        super().save_related(request, form, formsets, change)
        form.instance.recalculate_total(save=True)


class SaleItemInline(UnscopedAdminMixin, admin.TabularInline):
    model = models.SaleItem
    extra = 0
    fields = ["product", "medication_name", "category", "unit_price", "quantity", "line_total"]
    readonly_fields = ["line_total"]
    autocomplete_fields = ["product"]


@admin.register(models.Sale)
class SaleAdmin(UnscopedAdminMixin, admin.ModelAdmin):
    list_display = [
        "id",
        "customer",
        "payment_method",
        "total",
        "discounted_total",
        "created_by",
        "created_at",
    ]
    search_fields = ["customer__name", "customer__phone", "note"]
    list_filter = ["payment_method", "created_at"]
    autocomplete_fields = ["customer", "debt"]
    readonly_fields = ["total", "created_at", "updated_at"]
    inlines = [SaleItemInline]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalculate_total(save=True)


# <scaffold:admin>

from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm

from apps.store.modules import MODULES

User = get_user_model()


class UserAdminForm(UserChangeForm):
    """Checkbox picker for the modules THIS staff account may use.

    Unchecked = everything the store has (legacy default). The effective
    set is always intersected with the store's own subscription.
    """

    allowed_modules = forms.MultipleChoiceField(
        choices=[(k, f"{v} ({k})") for k, v in MODULES.items()],
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="اتركها كلها فارغة = كل وحدات الصيدلية متاحة لهذا الحساب.",
    )

    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = UserAdminForm
    list_display = (
        "username",
        "email",
        "store",
        "role",
        "phone",
        "display_name",
        "is_staff",
        "is_active",
        "date_joined",
    )
    list_filter = DjangoUserAdmin.list_filter + ("store", "role")
    search_fields = ("username", "email", "phone", "display_name")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Tenancy", {"fields": ("store", "role", "allowed_modules")}),
        ("Profile", {"fields": ("phone", "display_name", "avatar", "profile_image_url")}),
    )

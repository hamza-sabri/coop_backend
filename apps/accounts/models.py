from django.contrib.auth.models import AbstractUser
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.db import models
from django.db.models import Q


def avatar_upload_to(instance, filename: str) -> str:
    return f"avatars/{instance.pk or 'new'}/{filename}"


class User(AbstractUser):
    """Custom user model.

    Inherits the full Django auth stack (username login, permissions, groups)
    and adds a few common profile fields. This is the model you edit per
    project — add columns, add a custom manager, switch USERNAME_FIELD to
    email/phone, etc. Because AUTH_USER_MODEL points here from day one, those
    changes are cheap.
    """

    class Role(models.TextChoices):
        OWNER = "owner", "مالك الصيدلية"
        EMPLOYEE = "employee", "موظف"

    # USERNAME IS UNIQUE PER PHARMACY, NOT PER SYSTEM: two stores can both
    # have a "sara". Redeclared without unique=True; the real uniqueness lives
    # in Meta.constraints — (store, username), plus a separate constraint
    # keeping store-less platform-admin usernames globally unique so
    # createsuperuser / Django admin login stay unambiguous. Login is scoped
    # by the tenant domain's slug (apps.accounts.auth_backends), and the
    # auth.E003 system check is silenced deliberately in settings.
    username = models.CharField(
        "username",
        max_length=150,
        validators=[UnicodeUsernameValidator()],
        help_text="فريد داخل الصيدلية الواحدة (وليس عبر النظام).",
    )

    # Access level inside the store. OWNER sees everything the tenant has
    # (reports, imports, bulk ops); EMPLOYEE is day-to-day staff — POS, sales,
    # inventory, customers, debts. Existing accounts default to OWNER so
    # nothing is taken away without an explicit decision. Platform admins are
    # Django superusers, independent of this field.
    role = models.CharField(
        max_length=16, choices=Role.choices, default=Role.OWNER, db_index=True
    )

    # The tenant this staff account belongs to. Every API request derives its
    # store from HERE (never from client input) — a user with no store
    # gets no data access at all (platform admins use the Django admin).
    store = models.ForeignKey(
        "store.Store",
        related_name="staff",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    # Which feature modules THIS staff account may use, within whatever the
    # store itself has enabled (keys from apps.store.modules.MODULES).
    # EMPTY LIST = everything the store has. Lets an owner give a cashier
    # POS-only access, a bookkeeper debts-only, etc.
    allowed_modules = models.JSONField(default=list, blank=True)
    phone = models.CharField(max_length=20, blank=True, db_index=True)
    display_name = models.CharField(max_length=150, blank=True)
    # Uploaded profile image (stored on B2 when configured, else locally).
    avatar = models.ImageField(upload_to=avatar_upload_to, blank=True, null=True)
    # External profile image link (e.g. an OAuth/CDN avatar URL).
    profile_image_url = models.URLField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "users"
        verbose_name = "User"
        verbose_name_plural = "Users"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "username"],
                name="uniq_username_per_pharmacy",
            ),
            models.UniqueConstraint(
                fields=["username"],
                condition=Q(store__isnull=True),
                name="uniq_username_platform_admins",
            ),
        ]

    @property
    def is_owner(self) -> bool:
        """Store owner (or platform superuser) — full tenant access."""
        return self.is_superuser or self.role == self.Role.OWNER

    def __str__(self) -> str:
        return self.get_username()

from django.apps import AppConfig


class PharmacyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.store"
    verbose_name = "Store"

    def ready(self):
        from apps.store import signals  # noqa: F401 — connects the receivers

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    verbose_name = "Core"

    def ready(self):
        from django.db.models.signals import pre_save

        from apps.core.blurhash import strip_picture_fields

        # Clients echo back the URLs they were served, which carry "#bh=…".
        # Never store that fragment (any model, any picture field).
        pre_save.connect(strip_picture_fields, dispatch_uid="blurhash-strip")

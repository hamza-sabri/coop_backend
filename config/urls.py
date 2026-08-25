"""
Root URL configuration.

API surface:
    /admin/                 Django admin (Jazzmin-themed)
    /api/v1/auth/           Authentication (JWT)
    /api/v1/                Auto-discovered app routers
    /api/schema/            OpenAPI schema (drf-spectacular)
    /api/docs/              Swagger UI
    /api/docs/redoc/        ReDoc

Auto-discovery: any app under `apps.*` that ships a `urls.py` with a
`urlpatterns` list is mounted automatically under /api/v1/. This is why the
`setup_model` scaffolder never has to edit this file — creating the app's
urls.py is enough to wire it up.
"""
from importlib import import_module

from django.apps import apps as django_apps
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.core.views import healthz


def _discover_app_urls():
    """Include urls.py from every local app (except accounts, mounted below)."""
    patterns = []
    for app_config in django_apps.get_app_configs():
        name = app_config.name
        if not name.startswith("apps.") or name == "apps.accounts":
            continue
        try:
            module = import_module(f"{name}.urls")
        except ModuleNotFoundError:
            continue
        if getattr(module, "urlpatterns", None):
            patterns.append(path("api/v1/", include(f"{name}.urls")))
    return patterns


urlpatterns = [
    path("admin/", admin.site.urls),
    # Liveness probe (DB-free) — used by Docker/Dokploy/Traefik health checks
    path("healthz/", healthz, name="healthz"),
    # Auth (JWT)
    path("api/v1/auth/", include("apps.accounts.urls")),
    # OpenAPI / docs
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/docs/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

# Auto-mount scaffolded app routers.
urlpatterns += _discover_app_urls()

# Serve user-uploaded media locally in development (B2 handles it in prod).
if settings.DEBUG and not settings.STORAGE_ENABLED:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

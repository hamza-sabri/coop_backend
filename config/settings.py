"""
Django settings — single-file with conditional, optional integration blocks.

Design goals (the whole point of this template):
- The backend BOOTS AND RUNS with zero optional services configured.
- Each integration (Sentry, Backblaze B2, Redis/Celery) activates ONLY when
  its env vars are present, and degrades gracefully when they are not.
- Everything is driven by environment variables with sane defaults, loaded
  from a local .env in development.
"""
from datetime import timedelta
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
import os
import sys

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env (development convenience; in prod the platform provides real env).
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-CHANGE-ME-in-production")
DEBUG = env_bool("DEBUG", True)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "*" if DEBUG else "")

# Behind a proxy/load balancer (Railway, Render, Fly, etc.)
if env_bool("USE_X_FORWARDED_PROTO", not DEBUG):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    # Jazzmin must come before django.contrib.admin to theme it.
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "django_filters",
    "corsheaders",
    "simple_history",  # opt-in per model via `setup_model --history`
    # Local
    "apps.core",
    "apps.accounts",
    "apps.store",
    # <scaffold:local-apps>  (the `newapp` command registers new apps here)
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Records the acting user on history rows (for models using --history).
    "simple_history.middleware.HistoryRequestMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database — Postgres only (Neon in production). No sqlite, ever.
# DATABASE_URL is required; the app refuses to start without it.
#
# Exception: the `init` setup wizard runs BEFORE .env exists (it writes it), so
# settings must import for that one command using a throwaway placeholder that
# is never connected to.
# ---------------------------------------------------------------------------
_BOOTSTRAP = len(sys.argv) > 1 and sys.argv[1] == "init"
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    if _BOOTSTRAP:
        DATABASE_URL = "postgres://placeholder:placeholder@localhost:5432/placeholder"
    else:
        raise ImproperlyConfigured(
            "DATABASE_URL is required. Point it at your Postgres/Neon database, e.g.\n"
            "  postgres://user:pass@host/dbname?sslmode=require\n"
            "Tip: run `python manage.py init` to generate your .env."
        )
DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=int(os.getenv("DB_CONN_MAX_AGE", "600")),
        conn_health_checks=True,
        ssl_require=env_bool("DB_SSL_REQUIRE", False),
    )
}

# ---------------------------------------------------------------------------
# Uploads — catalogue/sales imports can be large. File parts of a multipart
# request are already exempt from DATA_UPLOAD_MAX_MEMORY_SIZE, but we lift it
# entirely so a big import is never rejected at the framework layer. Files
# above FILE_UPLOAD_MAX_MEMORY_SIZE spool to a temp file on disk instead of RAM.
# NOTE: a reverse proxy (Traefik/nginx) may still cap the body size — raise it
# there too if a very large upload is refused before it reaches Django.
# ---------------------------------------------------------------------------
DATA_UPLOAD_MAX_MEMORY_SIZE = None  # no framework-level request-body size limit
DATA_UPLOAD_MAX_NUMBER_FIELDS = None  # a wide sheet won't trip the field cap

# ---------------------------------------------------------------------------
# Custom user model
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

# Usernames are unique PER PHARMACY (tenant), not globally — login is scoped
# by the store slug the tenant site sends. PharmacyScopedBackend subclasses
# ModelBackend, so admin login and permission checks behave as stock Django.
AUTHENTICATION_BACKENDS = ["apps.accounts.auth_backends.PharmacyScopedBackend"]
# auth.E003/W004: "USERNAME_FIELD is not unique" — intentional (see
# accounts.User); uniqueness is enforced by the (store, username) and
# platform-admin constraints instead.
SILENCED_SYSTEM_CHECKS = ["auth.E003", "auth.W004"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# I18N / TZ
# ---------------------------------------------------------------------------
#: The hour the shop's day rolls over, in TIME_ZONE. 0 = midnight.
#:
#: A shop that trades past midnight does not close at midnight: everything rung
#: after 00:00 belongs to the day that is still going, because the owner cashes
#: up once, in the morning. Counting by calendar date splits one night's
#: takings across two figures and matches the drawer in neither.
#:
#: Default 0 keeps the ordinary case ordinary. A late-night shop sets 4 (or
#: whatever hour it actually cashes up at) and every "today" figure follows.
#:
#: TIME_ZONE must be the SHOP's zone for this to mean anything.
BUSINESS_DAY_START_HOUR = int(os.getenv("BUSINESS_DAY_START_HOUR", "0"))

LANGUAGE_CODE = os.getenv("LANGUAGE_CODE", "en-us")
TIME_ZONE = os.getenv("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static & media (WhiteNoise for static; media default = local, B2 if set)
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Default storage backends (overridden below if B2 is configured).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    # Locked-by-default: any endpoint that forgets to set permission_classes is
    # denied, not world-readable. The two intentionally public endpoints
    # (price check + marketing stats) set AllowAny explicitly.
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardPagination",
    "PAGE_SIZE": int(os.getenv("PAGE_SIZE", "30")),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.getenv("THROTTLE_ANON", "60/min"),
        "user": os.getenv("THROTTLE_USER", "240/min"),
        # Customer-facing price scans (per IP; a store's wifi is one IP,
        # so keep it generous enough for several shoppers scanning at once).
        "price_check": os.getenv("THROTTLE_PRICE_CHECK", "300/min"),
        # Background scan-count beacons from the kiosk — its OWN bucket so it
        # never eats into the price-check budget. Same order of magnitude.
        "scan_log": os.getenv("THROTTLE_SCAN_LOG", "300/min"),
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=int(os.getenv("ACCESS_TOKEN_LIFETIME_MINUTES", "60"))
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=int(os.getenv("REFRESH_TOKEN_LIFETIME_DAYS", "30"))
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": os.getenv("API_TITLE", "Backend API"),
    "DESCRIPTION": os.getenv(
        "API_DESCRIPTION", "API built from the django-backend-template."
    ),
    "VERSION": os.getenv("API_VERSION", "1.0.0"),
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1/",
    "COMPONENT_SPLIT_REQUEST": True,
}

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", DEBUG)
CORS_ALLOW_CREDENTIALS = env_bool("CORS_ALLOW_CREDENTIALS", True)

# Every store gets its own sub-domain (<slug>.clinixa.cloud), so an explicit
# origin list would need editing per customer — and leaving CORS wide open is
# not an option in production. A regex over the tenant root domain covers every
# current AND future tenant while still refusing everyone else.
_cors_root = os.getenv("PUBLIC_ROOT_DOMAIN", "clinixa.cloud").strip()
CORS_ALLOWED_ORIGIN_REGEXES = env_list("CORS_ALLOWED_ORIGIN_REGEXES") or [
    r"^https://([a-z0-9-]+\.)*" + _cors_root.replace(".", r"\.") + r"$"
]

# ===========================================================================
# OPTIONAL INTEGRATIONS — each activates only if configured
# ===========================================================================

# Root domain the tenant frontends live under (<slug>.<root>) — used to build
# the public price-page URL for the QR generator.
PUBLIC_ROOT_DOMAIN = os.getenv("PUBLIC_ROOT_DOMAIN", "clinixa.cloud")

# --- Backblaze B2 (S3-compatible) via django-storages -----------------------
B2_KEY_ID = os.getenv("B2_KEY_ID", "")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY", "")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "")
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL", "")  # e.g. https://s3.eu-central-003.backblazeb2.com
B2_REGION = os.getenv("B2_REGION", "")

STORAGE_ENABLED = bool(B2_KEY_ID and B2_APPLICATION_KEY and B2_BUCKET_NAME and B2_ENDPOINT_URL)
if STORAGE_ENABLED:
    AWS_ACCESS_KEY_ID = B2_KEY_ID
    AWS_SECRET_ACCESS_KEY = B2_APPLICATION_KEY
    AWS_STORAGE_BUCKET_NAME = B2_BUCKET_NAME
    AWS_S3_ENDPOINT_URL = B2_ENDPOINT_URL
    AWS_S3_REGION_NAME = B2_REGION or None
    AWS_DEFAULT_ACL = os.getenv("B2_DEFAULT_ACL", "private")
    AWS_S3_FILE_OVERWRITE = False
    AWS_QUERYSTRING_AUTH = env_bool("B2_QUERYSTRING_AUTH", True)  # signed URLs (private bucket)
    AWS_QUERYSTRING_EXPIRE = int(os.getenv("B2_URL_EXPIRE", str(7 * 24 * 3600)))
    STORAGES["default"] = {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"}

# --- Redis cache (optional; local-memory fallback) --------------------------
REDIS_URL = os.getenv("REDIS_URL", "")
REDIS_ENABLED = bool(REDIS_URL)
if REDIS_ENABLED:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                # Never let a Redis outage take down the API.
                "IGNORE_EXCEPTIONS": True,
            },
            "TIMEOUT": int(os.getenv("CACHE_TTL", str(60 * 60 * 24))),
        }
    }
    DJANGO_REDIS_IGNORE_EXCEPTIONS = True
else:
    # No Redis configured: in-process cache so cache.get/set still work.
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "django-backend-template",
        }
    }

# Sessions live in the DB (reliable) regardless of cache backend.
SESSION_ENGINE = "django.contrib.sessions.backends.db"

# --- Celery (optional; wired only if REDIS_URL / broker present) ------------
CELERY_ENABLED = bool(os.getenv("CELERY_BROKER_URL") or REDIS_URL)
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL") or REDIS_URL or ""
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

# --- Sentry (optional; inits only if DSN present) ---------------------------
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_ENABLED = bool(SENTRY_DSN)
if SENTRY_ENABLED:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[DjangoIntegration()],
            environment=os.getenv("SENTRY_ENVIRONMENT", "production" if not DEBUG else "development"),
            release=os.getenv("SENTRY_RELEASE") or None,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
            send_default_pii=env_bool("SENTRY_SEND_PII", False),
        )
    except Exception:  # pragma: no cover - never let Sentry break boot
        pass

# ---------------------------------------------------------------------------
# Production hardening (auto-on when DEBUG is False)
# ---------------------------------------------------------------------------
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# ---------------------------------------------------------------------------
# Jazzmin admin theme
# ---------------------------------------------------------------------------
JAZZMIN_SETTINGS = {
    "site_title": os.getenv("ADMIN_SITE_TITLE", "Backend Admin"),
    "site_header": os.getenv("ADMIN_SITE_HEADER", "Backend Admin"),
    "site_brand": os.getenv("ADMIN_SITE_BRAND", "Backend"),
    "welcome_sign": os.getenv("ADMIN_WELCOME", "Welcome"),
    "copyright": os.getenv("ADMIN_COPYRIGHT", ""),
    "search_model": ["accounts.User"],
    "show_ui_builder": DEBUG,
    "icons": {
        "auth.Group": "fas fa-users",
        "accounts.User": "fas fa-user",
    },
    "related_modal_active": True,
}
JAZZMIN_UI_TWEAKS = {
    "theme": os.getenv("ADMIN_THEME", "flatly"),
    "dark_mode_theme": None,
}

# ---------------------------------------------------------------------------
# Logging — console by default; Sentry captures errors when enabled.
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}


# ── Clerk (customer auth for the ordering app) ─────────────────────────────
# Blank = off, and the whole code path no-ops. Staff always sign in with Django
# users; Clerk never touches the admin.
#   CLERK_JWT_KEY makes verification networkless — a local RS256 check instead
#   of a round-trip to Clerk on a request a barista is waiting on. Set it.
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "")
CLERK_JWT_KEY = os.getenv("CLERK_JWT_KEY", "").replace("\\n", "\n").strip('"')
CLERK_AUTHORIZED_PARTIES = os.getenv("CLERK_AUTHORIZED_PARTIES", "")
CLERK_WEBHOOK_SIGNING_SECRET = os.getenv("CLERK_WEBHOOK_SIGNING_SECRET", "")
# Which tenant a Clerk signup belongs to. One shop today; when there are two,
# this becomes a lookup on the request host.
CLERK_STORE_SLUG = os.getenv("CLERK_STORE_SLUG", "koup")
SIGNUP_BONUS_BEANS = int(os.getenv("SIGNUP_BONUS_BEANS", "5") or 5)

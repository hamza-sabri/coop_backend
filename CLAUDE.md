# Pharma — Backend (`alrahmah`) — Session Summary

_Read this first when starting a session on the backend. It's a map, not a spec — the authoritative details live in `apps/store/models.py`, `serializers.py`, `views.py`, `urls.py`, and `API_DOCS.md`._

## What this is
The Django + DRF API behind **Pharma** (فارما), a multi-tenant SaaS POS / inventory / debts system for Palestinian stores. One backend serves many stores (tenants). The Next.js PWA in `../al-rahmah-store-admin` is the client.

## Stack
- **Django 5.2 + Django REST Framework**, `django-filter` for list filtering.
- **PostgreSQL (Neon, cloud)** — source of truth.
- **JWT auth** (SimpleJWT-style): access + refresh. `POST /api/v1/auth/login/`, `/auth/refresh/`, `/auth/logout/`, `/auth/me/`.
- **Backblaze B2** object storage for images/video. Stored values use a `b2://…` marker; helpers `store_upload()` (write) and `resolve_stored_url()` (sign on read) convert to signed URLs. Never store raw signed URLs.
- **Celery** (`config/celery.py`) for async/scheduled work.
- Deploy: Dockerfile + `entrypoint.sh` / `Procfile`; runs on Hostinger VPS via Dokploy. Single deployment (not per-tenant).

## Apps (`apps/`)
- **`accounts`** — users, auth, membership of a user in a store, roles (owner / employee).
- **`core`** — shared base classes / utilities.
- **`store`** — the domain. Key files:
  - `models.py` — all domain models (below).
  - `managers.py` — store-scoped managers / `StoreScopedMixin`.
  - `modules.py` — feature-module gating (which capabilities a store has).
  - `serializers.py` — DRF serializers (incl. `MedicationSerializer` with `image_files`/`image_urls` gallery, `SaleSerializer`).
  - `views.py` — viewsets + filters (e.g. `SaleFilter`, public price-check/branding views).
  - `reports.py` — inventory/sales report aggregations.
  - `importers.py` — Hesabate migration / bulk product import.
  - `cloning.py` — clone a tenant's data (demo tenant seeding).
  - `urls.py` — routes under `/api/v1/…`.
  - `management/commands/` — e.g. `seed_demo` (self-resetting demo tenant).
  - `tests/` — incl. `test_tenant_isolation.py` (isolation is CI-gated).

## Data model (`apps/store/models.py`)
`Plan`, **`Store`** (the tenant), `Category`, `Manufacturer`, `CatalogItem` / `CatalogItemImage`, **`Product`** / `ProductVariant` / `ProductImage`, `Customer`, **`Debt`** / `DebtItem`, **`Sale`** / `SaleItem`, `PosCartState`.
- **Every tenant-owned row has a `store` FK.** This is the isolation backbone.
- `Sale` has a **`client_uuid`** idempotency key — the offline client sends it so a retried POST never double-creates a sale or double-decrements stock. Preserve this.
- Stock is derived/adjusted by sales; treat it as server-authoritative.

## Multi-tenancy — the #1 rule
**Strict tenant isolation.** Every query must be scoped to the requesting user's store (via `StoreScopedMixin` / scoped managers / queryset filtering). A tenant must never see another tenant's rows. There is a dedicated test suite (`test_tenant_isolation.py`) and a CI gate — **run it after any query/serializer/view change.** Public endpoints (below) are the only unauthenticated surface and must expose **only** non-sensitive fields.

## Feature modules (`modules.py`)
Capabilities are gated per store: `inventory`, `pos`, `customers`, `debts`, `price_check`, `imports` (+ `reports`, `sales_reports`, `offline`). The frontend mirrors this to lock/unlock UI. When adding a feature, gate it here.

## Public (unauthenticated) endpoints
- `GET /api/v1/public/branding/?store=<slug>` → `{ name, logo }` (white-label). **No server-side cache that outlives a name change** — the client caches it (see branding note below).
- `GET /api/v1/public/branding/icon/?store=<slug>&size=192|512[&maskable=1]` → square PNG for the PWA manifest.
- `GET /api/v1/public/price-check/?store=<slug>&barcode=…` (or `&q=…`) → `{ found, name, price, image }` and, only when present, `images[]`, `video_url`, `variants`. **Extras must appear only when they exist** (the isolation test asserts no field leaks).

## Conventions / footguns
- **Isolation first:** add `store` scoping to every new model/query; extend `test_tenant_isolation.py`.
- **Idempotency:** keep `client_uuid` on any offline-createable resource (sales; future: debt payments, inventory edits).
- **Storage:** images/video via `store_upload` / `resolve_stored_url` (`b2://` markers), never raw URLs.
- **Public leak safety:** public serializers expose a strict allowlist of fields.
- **Branding:** if you ever add server caching to the branding view, add invalidation on `Store` save — the client already caches aggressively (see frontend note), so a stale server too = names that never update.

## Related frontend context
See `../al-rahmah-store-admin/CLAUDE.md`. The client is offline-capable (IndexedDB mirror + idempotent sale queue), white-labels per tenant via the public branding endpoint, and is deployed per-store-branch. The offline sync roadmap (two-valve upload/download controls) is in `../PHARMA_WORK_PLAN.md`.

## Run / test
- Tests: `python manage.py test` (ensure `test_tenant_isolation` passes — CI-gated).
- Migrations: `python manage.py makemigrations && migrate`.
- Demo tenant: `python manage.py seed_demo` (self-resetting).
- API reference: `API_DOCS.md`, setup: `ALRAHMAH_SETUP.md` / `README.md`.

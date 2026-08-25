# AL-Rahmah Store — backend

Built on the django-backend-template. Domain app: `apps/store` (Product,
Customer, Debt, DebtItem). Every endpoint **requires login** (JWT) — this is an
internal tool holding customer PII and debts.

## Run it (locally, against your Neon DB)

The `.env` is already written and points at your Neon database. From this folder:

```bash
./bootstrap.sh          # venv + deps + migrate + import 21k meds + create admin
python manage.py runserver
```

`bootstrap.sh` is idempotent — re-running only adds what's new. If you prefer to
do it by hand:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py import_meds          # loads data/price-list.xlsx (~21,343 rows)
python manage.py createsuperuser
python manage.py runserver
```

> I couldn't run migrate/import from here (this sandbox has no network route to
> Neon), so the DB is still empty — running `./bootstrap.sh` on your machine
> fills it. Everything was validated end-to-end against a throwaway local DB
> (all 21,343 rows import cleanly; all API behaviour below passes).

- Swagger: `/api/docs/` · ReDoc: `/api/docs/redoc/` · Admin: `/admin/`
- Login: `POST /api/v1/auth/login/` → `{access, refresh, user}`, then send
  `Authorization: Bearer <access>`.

## Endpoints

| Resource | URL | Notes |
|---|---|---|
| Medications | `/api/v1/products/` | catalogue: name, barcode, price, cost, brand, manufacturer, category, stock, notes, image |
| Customers | `/api/v1/customers/` | profiles (no login): name, phone, avatar, notes, status, `outstanding` |
| Debts | `/api/v1/debts/` | per customer, with med line items + totals |

All three support **search** (`?search=`), **filter**, and **sort**
(`?ordering=field` / `?ordering=-field` for descending), paginated 30/page
(`?page=`, `?page_size=` up to 100).

- Medications — search: name, barcode, brand, manufacturer, category · filter:
  `?category=` `?brand=` `?manufacturer=` `?barcode=` · sort: name, price, cost, stock…
- Customers — search: name, phone, status, notes · filter: `?status=` `?phone=` ·
  sort: name, created_at… · `status` is free text, indexed and searchable.
- Debts — search: customer name/phone, note · filter: `?customer=<id>` `?is_paid=true|false` ·
  sort: total, discounted_total, created_at…

## Images (avatar / med image) — one call handles it

Each image is stored as a **URL** (`avatar` on Customer, `image` on Product).
You can either send that URL directly, **or** upload a file and the API stores it
(Backblaze B2 when configured, else local `/media`) and saves the resulting URL —
same create/edit request:

```bash
# set a URL directly
curl -X POST .../api/v1/customers/ -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" \
  -d '{"name":"Ahmad","phone":"0599...","avatar":"https://cdn/x.png","status":"regular"}'

# or upload a file -> stored -> URL saved to `avatar`
curl -X POST .../api/v1/customers/ -H "Authorization: Bearer $T" \
  -F name=Ahmad -F phone=0599... -F avatar_file=@/path/photo.jpg
```

Med image works the same via the `image_file` field.

## Debts

Create a debt with the meds bought; the server computes everything:

```bash
curl -X POST .../api/v1/debts/ -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" -d '{
    "customer": 1,
    "items": [
      {"product": 42, "quantity": 2},
      {"product": 99, "quantity": 1},
      {"medication_name": "Custom item", "unit_price": "12.50", "quantity": 3}
    ]
  }'
```

- `total` = sum of `unit_price × quantity` across items, computed server-side and
  **read-only** (sending it is ignored).
- Each item **snapshots** the med's name and price at purchase time, so later
  price changes never rewrite an existing debt.
- Give `product` (id) and the price is taken from the med; or give
  `medication_name` + `unit_price` for a free-text line.
- `discounted_total` defaults to `total`; `PATCH` it to apply a discount.
- `is_paid` marks a debt settled. Each customer exposes `outstanding` = sum of
  `discounted_total` across their **unpaid** debts.

## import_meds

```bash
python manage.py import_meds                 # data/price-list.xlsx (default)
python manage.py import_meds --path x.xlsx
python manage.py import_meds --clear         # wipe meds, then re-import
python manage.py import_meds --dry-run
```

Maps the Arabic columns → name, cost, barcode, brand, manufacturer, stock,
category, notes, and retail price (first number in `مفرق`). `image` is left
blank (fill later). Idempotent by `source_id`. Tiered prices (e.g.
`علبة 30 شيكل شريط 5 شيكل`) keep the first as `price` and the full text in
`notes`; a couple of rows had a barcode in the price cell — those are set to 0
and the raw text kept in `notes`.

## When you have the keys (Redis / B2 / Sentry)

Everything degrades gracefully — the backend runs fine with these blank. Set them
in `.env` (local) or the Dokploy dashboard (prod) to switch each on:

- **Redis** — `REDIS_URL=redis://…` → caching (and Celery broker) light up.
- **Backblaze B2** — set all of `B2_KEY_ID`, `B2_APPLICATION_KEY`,
  `B2_BUCKET_NAME`, `B2_ENDPOINT_URL` (+ `B2_REGION`) → uploads go to B2 and image
  URLs become public B2 links automatically. No code change.
- **Sentry** — `SENTRY_DSN=…` → error tracking on.

For Dokploy deploy: set `SECRET_KEY`, `DATABASE_URL`, `DEBUG=False`, `DOMAIN`
(+ optional keys) in the dashboard. The container migrates on start; run
`python manage.py import_meds` once as a one-off if you import on the server
instead of locally.

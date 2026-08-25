# AL-Rahmah Store — API reference

Base URL (local): `http://127.0.0.1:8000`
All app data lives under `/api/v1/`. Interactive docs: `/api/docs/` (Swagger),
`/api/docs/redoc/` (ReDoc), raw schema: `/api/schema/`.

## Authentication

Every endpoint requires a logged-in account (JWT). There is **no public
registration** — accounts are created by staff via the Django admin
(`/admin/`) or `python manage.py createsuperuser`.

Flow: `POST /auth/login/` → you get an `access` token (valid 60 min) and a
`refresh` token (valid 30 days, rotates). Send the access token on every request:

```
Authorization: Bearer <access>
```

When a request returns `401`, exchange the refresh token at `/auth/refresh/` for
a new access token and retry.

### POST /api/v1/auth/login/

Request:

```json
{ "username": "admin", "password": "your-password" }
```

Response `200`:

```json
{
  "access": "<jwt access token>",
  "refresh": "<jwt refresh token>",
  "user": {
    "id": 1, "username": "admin", "email": "admin@alrahmah.test",
    "first_name": "", "last_name": "", "phone": "", "display_name": "",
    "avatar": null, "profile_image_url": "", "is_staff": true,
    "date_joined": "2026-07-01T10:00:00Z"
  }
}
```

Wrong credentials → `401 {"detail": "No active account found with the given credentials"}`.

### POST /api/v1/auth/refresh/

Request `{ "refresh": "<refresh>" }` → `200 { "access": "<new access>", "refresh": "<new refresh>" }`.

### POST /api/v1/auth/logout/

Blacklists a refresh token. Requires auth.
Request `{ "refresh": "<refresh>" }` → `205` (no body). Invalid token → `400`.

### GET /api/v1/auth/me/  ·  PATCH /api/v1/auth/me/

Read or update the current account. `GET` → the `user` object above.
`PATCH` accepts `email`, `first_name`, `last_name`, `phone`, `display_name`,
`profile_image_url`, and `avatar` (file upload, multipart).

---

## Conventions (all list endpoints)

**Pagination** — every list is paginated (default 30/page, max 100):

```
GET /api/v1/products/?page=2&page_size=50
```

```json
{
  "count": 21343,
  "total_pages": 712,
  "current_page": 2,
  "page_size": 30,
  "next": "http://127.0.0.1:8000/api/v1/products/?page=3",
  "previous": "http://127.0.0.1:8000/api/v1/products/?page=1",
  "results": [ /* ... */ ]
}
```

**Search** — `?search=` matches any of the resource's search fields (see each
below). Works with Arabic text.

**Filter** — exact-match query params per resource, e.g. `?category=ادوية`.

**Sort** — `?ordering=<field>` ascending, `?ordering=-<field>` descending, e.g.
`?ordering=-price`. Combine freely with search/filter/pagination.

**Errors** — `400` validation returns `{ "field": ["message"], ... }`;
`401` → `{"detail": "Authentication credentials were not provided."}`;
`404` → `{"detail": "No Product matches the given query."}`.

**Money** — `price`, `cost`, `total`, `discounted_total`, `unit_price`,
`line_total` are decimal **strings** with 2 places, e.g. `"35.00"`. Currency is
the Israeli shekel (₪ / شيكل). Max value ~9,999,999,999.99.

**Images** — `image` (med) and `avatar` (customer) are stored as **URLs**. On
create/edit you may either send the URL directly, **or** upload a file via the
matching `*_file` field (`image_file` / `avatar_file`, multipart). Uploaded files
go to storage (Backblaze B2 when configured, else local `/media`) and the
resulting URL is saved automatically — one request either way.

---

## Medications — `/api/v1/products/`

| Field | Type | Notes |
|---|---|---|
| `id` | int | read-only |
| `source_id` | string | id from the source price list |
| `name` | string | required |
| `barcode` | string | |
| `price` | decimal string | retail/selling price (used on debts) |
| `cost` | decimal string | |
| `brand` | string | |
| `manufacturer` | string | |
| `category` | string | e.g. `ادوية`, `كوزمتكس`, `عطر` |
| `stock` | int | may be negative |
| `notes` | string | |
| `image` | URL string | blank by default |
| `image_file` | file | **write-only**; upload → stored → URL saved to `image` |
| `created_at` / `updated_at` | datetime | read-only |

- **Search** (`?search=`): name, barcode, brand, manufacturer, category, source_id
- **Filter**: `?category=` `?brand=` `?manufacturer=` `?barcode=` `?source_id=`
- **Sort** (`?ordering=`): name, price, cost, stock, created_at, updated_at

Endpoints: `GET` (list), `POST` (create), `GET /{id}/`, `PUT/PATCH /{id}/`,
`DELETE /{id}/`.

Examples:

```
GET /api/v1/products/?search=panadol&category=ادوية&ordering=-price&page=1
```

```bash
# create with an image URL
curl -X POST .../api/v1/products/ -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" \
  -d '{"name":"Panadol Extra","barcode":"123456","price":"18.00","cost":"12.00","category":"ادوية","stock":40}'

# create/patch with an uploaded picture (multipart)
curl -X PATCH .../api/v1/products/42/ -H "Authorization: Bearer $T" \
  -F image_file=@/path/box.jpg
```

Response `200/201` (one med):

```json
{
  "id": 42, "source_id": "1042", "name": "Panadol Extra", "barcode": "123456",
  "price": "18.00", "cost": "12.00", "brand": "GSK", "manufacturer": "",
  "category": "ادوية", "stock": 40, "notes": "",
  "image": "https://.../media/products/ab12.jpg",
  "created_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-01T10:05:00Z"
}
```

### GET /api/v1/products/stats/

Catalogue KPIs (computed in the DB — cheap even for the full catalogue):

```json
{
  "total_items": 21343,
  "in_stock": 8210,
  "out_of_stock": 13133,
  "total_units": 45120,
  "retail_value": "612340.00",
  "cost_value": "410220.00",
  "by_category": [ { "category": "كوزمتكس", "count": 9012 }, { "category": "ادوية", "count": 5781 } ]
}
```

`retail_value`/`cost_value` = Σ(price·stock) / Σ(cost·stock) over in-stock items;
`by_category` = top 8 categories by item count.

---

## Customers — `/api/v1/customers/`

Customer profiles. Managed by staff; customers never log in.

| Field | Type | Notes |
|---|---|---|
| `id` | int | read-only |
| `name` | string | required |
| `phone` | string | |
| `gender` | string | `"male"` or `"female"` — defaults to `"male"` |
| `avatar` | URL string | |
| `avatar_file` | file | **write-only**; upload → stored → URL saved to `avatar` |
| `notes` | string | |
| `status` | string | free text, searchable/filterable (e.g. `منتظم`, `جديد`) |
| `outstanding` | decimal string | read-only: sum of `discounted_total` across the customer's **unpaid** debts |
| `created_at` / `updated_at` | datetime | read-only |

- **Search** (`?search=`): name, phone, status, notes
- **Filter**: `?status=` `?phone=` `?gender=male|female`
- **Sort** (`?ordering=`): name, phone, created_at, updated_at

Endpoints: `GET`, `POST`, `GET /{id}/`, `PUT/PATCH /{id}/`, `DELETE /{id}/`.

```
GET /api/v1/customers/?search=0599123456
GET /api/v1/customers/?status=منتظم&ordering=name
```

```json
{
  "id": 7, "name": "أحمد يوسف", "phone": "0599123456",
  "avatar": "https://cdn.example.com/a.png", "notes": "زبون دائم",
  "status": "منتظم", "outstanding": "125.50",
  "created_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-01T10:00:00Z"
}
```

To see a customer's debts, list debts filtered by that customer:
`GET /api/v1/debts/?customer=7`.

---

## Debts — `/api/v1/debts/`

A debt = the meds a customer bought, plus totals.

| Field | Type | Notes |
|---|---|---|
| `id` | int | read-only |
| `customer` | int (id) | required; the customer who owes |
| `customer_name` | string | read-only (convenience) |
| `customer_phone` | string | read-only (convenience) |
| `items` | array | the med lines (see below); optional |
| `amount` | decimal string | **write-only**, optional. For an item-less debt ("customer owes 100") send `amount` with no `items`; the server sets `total` from it. Ignored when `items` are provided. |
| `total` | decimal string | **read-only**. Computed from `items` when present, else set from `amount`. Sending it is ignored. |
| `discounted_total` | decimal string | editable; defaults to `total`. Send to apply a discount. |
| `is_paid` | bool | mark the debt settled |
| `note` | string | |
| `created_at` / `updated_at` | datetime | read-only |

Two ways to create a debt:

```jsonc
// A) itemized — total computed from the meds
{ "customer": 7, "items": [ {"product": 42, "quantity": 2} ] }

// B) direct amount — no meds; total = amount
{ "customer": 7, "amount": "100.00", "note": "دفعة سابقة" }
```

Each **item** (`items[]`):

| Field | Type | Notes |
|---|---|---|
| `id` | int | read-only |
| `product` | int (id) or null | pick a med; its name & price are snapshotted |
| `medication_name` | string | auto-filled from the med, or set it for a free-text line |
| `unit_price` | decimal string | optional when `product` is given (taken from the med); required for a free-text line |
| `quantity` | int | default 1 |
| `line_total` | decimal string | **read-only**, = `unit_price` × `quantity` |

Snapshots: an item freezes the med's name and price at purchase time, so later
catalogue price changes never rewrite an existing debt.

- **Search** (`?search=`): customer name, customer phone, note
- **Filter**: `?customer=<id>` `?is_paid=true|false`
- **Sort** (`?ordering=`): total, discounted_total, is_paid, created_at, updated_at

Endpoints: `GET`, `POST`, `GET /{id}/`, `PUT/PATCH /{id}/`, `DELETE /{id}/`.

Create:

```bash
curl -X POST .../api/v1/debts/ -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" -d '{
    "customer": 7,
    "items": [
      { "product": 42, "quantity": 2 },
      { "product": 88, "quantity": 1 },
      { "medication_name": "خلطة خاصة", "unit_price": "12.50", "quantity": 3 }
    ],
    "note": "دفعة أولى لاحقاً"
  }'
```

Response `201`:

```json
{
  "id": 15, "customer": 7, "customer_name": "أحمد يوسف", "customer_phone": "0599123456",
  "items": [
    { "id": 31, "product": 42, "medication_name": "Panadol Extra", "unit_price": "18.00", "quantity": 2, "line_total": "36.00" },
    { "id": 32, "product": 88, "medication_name": "Vitamin C", "unit_price": "20.00", "quantity": 1, "line_total": "20.00" },
    { "id": 33, "product": null, "medication_name": "خلطة خاصة", "unit_price": "12.50", "quantity": 3, "line_total": "37.50" }
  ],
  "total": "93.50", "discounted_total": "93.50", "is_paid": false,
  "note": "دفعة أولى لاحقاً",
  "created_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-01T10:00:00Z"
}
```

Apply a discount / mark paid (PATCH):

```bash
curl -X PATCH .../api/v1/debts/15/ -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" -d '{ "discounted_total": "80.00", "is_paid": true }'
```

Update the items (PATCH with `items` replaces all lines and recomputes `total`;
if you don't also send `discounted_total`, it resets to the new `total`).
`total` can never be set directly.

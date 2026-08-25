# v0 prompt — AL-Rahmah Store admin (paste this into v0)

> Copy everything below the line into v0. It's self-contained: it includes the
> full API contract, so v0 doesn't need any other file.

---

Build a **mobile-first, responsive admin web app** for a store called
**AL-Rahmah (الرحمة)**. It's an internal tool for store staff to manage
**products, customers, and their debts**, plus a **stats dashboard**. It talks
to an **already-deployed** Django REST API (contract below). Optimize for
**phones first**, then scale up to tablet/desktop.

## Backend — live, deployed

- Base URL: **https://alrahmah.clinixa.cloud** (HTTPS).
- Swagger: `https://alrahmah.clinixa.cloud/api/docs/`
- **OpenAPI schema: `https://alrahmah.clinixa.cloud/api/schema/`** (used by orval).
- Every endpoint requires a JWT **except** login/refresh. There is **no signup**.

## Stack & setup

- **Next.js (App Router) + TypeScript + Tailwind + shadcn/ui.**
- Initialize shadcn with the **Midnight Bloom** theme first:
  ```
  pnpm dlx shadcn@latest init "https://shadcnstudio.com/r/themes/midnight-bloom.json"
  ```
  Use this theme's tokens/colors throughout; don't hardcode other palettes.
- **Data layer: orval** — generate a typed client + TanStack Query hooks from the
  live OpenAPI schema (see "API layer"). **Do not hand-write fetch/axios calls.**
- **Animations: GSAP** (`gsap` + `@gsap/react`'s `useGSAP`).
- **Charts: shadcn/ui charts (Recharts)** for the debt dashboard.
- Forms: **react-hook-form + zod**. Toasts: shadcn **sonner**. Icons: **lucide-react**.
- API base URL from `process.env.NEXT_PUBLIC_API_BASE_URL`, default
  `https://alrahmah.clinixa.cloud`. Put it in `.env.local`.

## API layer — orval (keep API calls organized & typed)

Generate the entire API client and React-Query hooks from the live schema, then
use those hooks everywhere. Add an `orval.config.ts`:

```ts
import { defineConfig } from "orval";

export default defineConfig({
  alrahmah: {
    input: "https://alrahmah.clinixa.cloud/api/schema/",
    output: {
      mode: "tags-split",              // one folder per tag: auth, products, customers, debts
      target: "src/api/generated",
      schemas: "src/api/generated/model",
      client: "react-query",
      httpClient: "fetch",
      clean: true,
      override: {
        mutator: { path: "src/api/http.ts", name: "customFetch" },
        query: { useInfinite: true, useInfiniteQueryParam: "page" },
      },
    },
  },
});
```

Add a script `"api": "orval"` and run `pnpm api` to (re)generate into
`src/api/generated`. Custom fetch mutator `src/api/http.ts` — prepend the base
URL, attach the JWT, and refresh once on 401:

```ts
export const customFetch = async <T>(url: string, options: RequestInit = {}): Promise<T> => {
  const base = process.env.NEXT_PUBLIC_API_BASE_URL ?? "https://alrahmah.clinixa.cloud";
  const withAuth = (): RequestInit => ({
    ...options,
    headers: {
      ...options.headers,
      ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
    },
  });
  let res = await fetch(base + url, withAuth());
  if (res.status === 401 && (await tryRefresh())) res = await fetch(base + url, withAuth());
  if (res.status === 401) { redirectToLogin(); throw new Error("Unauthorized"); }
  if (!res.ok) throw await res.json().catch(() => new Error(res.statusText));
  return (res.status === 204 ? undefined : await res.json()) as T;
};
```

Then import the generated hooks (names follow the schema's operationIds, e.g.
`useMedicationsList`, `useCustomersCreate`, `useDebtsList`, `useDebtsPartialUpdate`)
directly in components — no bespoke data code.

## Language & direction — Arabic RTL

- Entire UI is **Arabic, right-to-left**: `<html lang="ar" dir="rtl">`, use logical
  spacing (`ms-*`, `me-*`).
- Clean Arabic web font via `next/font` (e.g. **IBM Plex Sans Arabic**, **Cairo**,
  or **Noto Kufi Arabic**).
- Currency is the shekel: show amounts like `93.50 ₪`.
- Arabic labels:
  - Nav/actions: لوحة التحكم (Dashboard), الأدوية (Medications), الزبائن (Customers),
    الديون (Debts), تسجيل الدخول (Login), تسجيل الخروج (Logout), بحث (Search),
    إضافة (Add), تعديل (Edit), حذف (Delete), حفظ (Save), إلغاء (Cancel),
    تصفية (Filter), ترتيب (Sort).
  - Product: الاسم, الباركود, السعر, التكلفة, العلامة التجارية, الشركة المنتجة,
    التصنيف, الكمية, ملاحظات, الصورة.
  - Customer: الاسم, رقم الهاتف, الجنس (gender: ذكر=male / أنثى=female),
    الصورة, الحالة, ملاحظات, الرصيد المستحق (outstanding).
  - Debt: الزبون, الأصناف, الإجمالي, الإجمالي بعد الخصم, مدفوع, ملاحظة,
    الكمية, سعر الوحدة.

## Auth (login only — NO signup, NO user creation)

- A single **`/login`** page (username + password). No signup/register page and no
  UI to create users anywhere.
- Store `access` + `refresh` tokens; send `Authorization: Bearer <access>` on every
  call (handled in the orval mutator). On 401 → refresh once → retry → else
  `/login`.
- **All app routes protected.** Show the logged-in user's name and a **logout**
  action (`POST /auth/logout/`, then clear tokens → `/login`).

## App shell / navigation (mobile-first)

- **Mobile**: fixed **bottom tab bar** — لوحة التحكم (Dashboard), الأدوية, الزبائن,
  الديون. Big tap targets, sticky top search on list screens, floating **+ Add** button.
- **Desktop/tablet**: same sections as a right-side (RTL) sidebar; lists may become
  tables.
- Global: loading **skeletons**, error+retry states, empty states, optimistic toasts,
  confirm dialog before delete.

## Screens

### 0) Dashboard — debt stats (لوحة التحكم)
A stats overview built from the debts (and customers) data. Aggregate client-side
by pulling pages with `?page_size=100` (loop until `next` is null); a dedicated
stats endpoint can be added later if needed. Include:

- **Stat cards** (animate the numbers with a GSAP count-up): total **outstanding**
  (Σ `discounted_total` where `is_paid=false`), total **collected** (Σ where
  `is_paid=true`), **# customers**, **# unpaid debts**.
- **Donut** — paid vs unpaid debts.
- **Bar or line** — debts (or outstanding amount) **per month**, from `created_at`.
- **Bar** — **top debtors**: customers ranked by outstanding.
- **Donut** — **customers by gender** (male/female) using the new gender field.

Use shadcn/ui charts (Recharts). Animate chart/section entrances with GSAP.

### 1) Medications (الأدوية)
- Searchable/filterable/sortable list (~21k rows — paginate/infinite scroll via
  `?page=`/`?page_size=`). Search → `?search=` (Arabic, debounced ~300ms). Filters:
  category, brand. Sort: name, price, stock (asc/desc). Row/card: name, category,
  price ₪, stock, thumbnail if `image`.
- Create/Edit form: name, barcode, price, cost, brand, manufacturer, category, stock,
  notes, and an **image** field that accepts **either a URL or a file upload** (see
  Images). Delete with confirm.

### 2) Customers (الزبائن)
- List: search by **name or phone** (`?search=`), filter by **status** and **gender**
  (`?gender=male|female`), sort by name. Card: name, phone, **gender badge**, status
  badge, **outstanding** ₪.
- Create/Edit form: name, phone, **gender** (radio: ذكر/أنثى, **default ذكر/male**),
  status (free text), notes, and an **avatar** (URL or file upload).
- Customer **detail**: profile header (avatar, name, phone, gender, status,
  outstanding) + that customer's **debts** (`/debts/?customer=<id>`) with an "add
  debt" button.

### 3) Debts (الديون)
- List: filters customer + paid/unpaid (`?is_paid=`), search by customer name/phone,
  sort by date/total. Card: customer name, total (or discounted), paid/unpaid badge,
  date.
- **Create debt**: pick a customer, add **line items** — for each, autocomplete a
  product (`/products/?search=`) + quantity; unit price auto-fills from the med
  but is editable. Also allow a **free-text line** (name + unit price). Show a **live
  running total**.
- Optional **discount** (edit `discounted_total`; defaults to `total`). `total` is
  **read-only** — never editable directly.
- Detail/Edit: view items+totals; toggle **paid**; adjust discount; edit items.

## Images — one field, URL or upload

For med `image` and customer `avatar`: the form field accepts a URL **or** a file
upload. If a file is chosen, send it multipart on the `*_file` field (`image_file`
/ `avatar_file`); the API stores it and returns the saved URL. If only a URL is
given, send it on the plain field. Preview the current image.

## Animations — GSAP

Use `@gsap/react`'s `useGSAP` for: route/page transitions, staggered list-item
reveals, the FAB, dashboard **number count-ups**, and chart entrances. Keep it
subtle (200–400ms), performant on mobile, and honor `prefers-reduced-motion`.

## Non-goals (do not build)
- No signup/registration, no "create user"/user-management UI.
- No public/marketing pages — logged-in tool only.

---

# API contract

Base: `NEXT_PUBLIC_API_BASE_URL` (default `https://alrahmah.clinixa.cloud`). All data
under `/api/v1/`. Every endpoint needs `Authorization: Bearer <access>` except
login/refresh. **Prefer the orval-generated hooks over these raw paths** — they're
listed so you know the shapes.

**Auth**
- `POST /api/v1/auth/login/` — `{username, password}` → `{access, refresh, user}`.
- `POST /api/v1/auth/refresh/` — `{refresh}` → `{access, refresh}`.
- `POST /api/v1/auth/logout/` — `{refresh}` → `205`.
- `GET/PATCH /api/v1/auth/me/` — current account. (No register endpoint.)

**List conventions** — paginated + search/filter/sort:
```
GET /api/v1/<resource>/?search=<q>&ordering=<field|-field>&page=<n>&page_size=<m>
```
Envelope: `{ count, total_pages, current_page, page_size, next, previous, results }`.
Money fields are 2-dp **strings** (e.g. `"35.00"`).

**Medications** `/api/v1/products/` — `GET`(list) `POST` `GET/PUT/PATCH/DELETE /{id}/`
- Fields: `id`, `source_id`, `name`, `barcode`, `price`, `cost`, `brand`,
  `manufacturer`, `category`, `stock`, `notes`, `image` (URL),
  `image_file` (write-only upload → sets `image`), `created_at`, `updated_at`.
- search: name, barcode, brand, manufacturer, category · filters: `category`,
  `brand`, `manufacturer`, `barcode` · ordering: name, price, cost, stock, created_at.

**Customers** `/api/v1/customers/` — `GET` `POST` `GET/PUT/PATCH/DELETE /{id}/`
- Fields: `id`, `name`, `phone`, **`gender`** (`"male"` | `"female"`, **default
  `"male"`**), `avatar` (URL), `avatar_file` (write-only upload → sets `avatar`),
  `notes`, `status`, `outstanding` (read-only, Σ unpaid debts), `created_at`,
  `updated_at`.
- search: name, phone, status, notes · filters: `status`, `phone`, **`gender`** ·
  ordering: name, phone, created_at.

**Debts** `/api/v1/debts/` — `GET` `POST` `GET/PUT/PATCH/DELETE /{id}/`
- Fields: `id`, `customer` (id), `customer_name` (ro), `customer_phone` (ro),
  `items[]`, `total` (**read-only**, computed), `discounted_total` (editable,
  defaults to total), `is_paid` (bool), `note`, `created_at`, `updated_at`.
- `items[]`: `{ id, product (id|null), medication_name, unit_price, quantity, line_total(ro) }`.
  Give `product` and price auto-fills; or give `medication_name` + `unit_price`
  for a free-text line. `quantity` default 1.
- search: customer name/phone, note · filters: `customer`, `is_paid` · ordering:
  total, discounted_total, created_at.

Create debt body:
```json
{ "customer": 7, "items": [ {"product": 42, "quantity": 2}, {"medication_name": "خلطة", "unit_price": "12.50", "quantity": 3} ], "note": "" }
```
Response includes computed `total`, `discounted_total`, and each `line_total`.
PATCH `{ "discounted_total": "80.00", "is_paid": true }` to discount / settle.
Sending `total` is ignored.

## Acceptance
- API layer is **orval-generated** hooks over the live schema (no hand-written fetch).
- Login works; all routes protected; 401 auto-refresh; logout clears session.
- Arabic RTL throughout; great on a phone; scales to desktop.
- **Dashboard** shows debt stats with **charts** and **GSAP** animations.
- Meds: search/filter/sort a large list; create/edit with image URL **or** upload.
- Customers: search by name/phone; filter by status **and gender**; create/edit with
  **gender** (default male) and avatar URL **or** upload; detail shows their debts.
- Debts: build from med line items with a live total; optional discount; mark paid;
  `total` never editable.
- No signup and no user-creation UI anywhere.

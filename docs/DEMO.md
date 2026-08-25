# Public Demo Tenant — setup & nightly reset

A self-contained **`demo`** store lets anyone try Pharma without touching real
data. Everything is one tenant, so it can be wiped and reseeded nightly safely.

## 1. Provision (once, after deploy)

```bash
python manage.py migrate
python manage.py seed_demo --reset --seed 7
```

Creates:
- Store `slug="demo"` (صيدلية فارما التجريبية)
- Login **`demo / demo`** (all modules enabled)
- ~40 Arabic meds (categories, manufacturers, stock), ~120 customers,
  ~300 debts (itemised + plain, paid/unpaid), ~500 POS sales spread over 90 days
  so the dashboard and charts have shape.

**Safety:** `--reset` deletes **only** the `demo` tenant's rows. It never reads
or deletes any other store's data (verified by the query scoping — every
delete is `filter(store_id=demo)`).

## 2. Where the demo lives

- Admin app: `https://demo.pharma.ps` (or the current `alrahmah.clinixa.cloud`
  with the `demo` tenant) → login `demo / demo`.
- Public price-check (no login): `…/price?store=demo` — scan any seeded
  barcode (e.g. `6001082000019` بنادول) to see name + price + image.

## 3. Nightly reset (keep it clean)

Run the reseed every night so visitors always get a pristine demo. Pick one:

**A — Dokploy scheduled job (recommended):** add a scheduled command on the
backend service:
```
0 3 * * *  python manage.py seed_demo --reset --seed 7
```

**B — VPS cron** (runs inside the backend container/venv, with `DATABASE_URL` set):
```bash
0 3 * * * cd /app && /app/.venv/bin/python manage.py seed_demo --reset --seed 7 >> /var/log/pharma-demo.log 2>&1
```

`--seed 7` makes the dataset deterministic (same demo every night). Drop it for
fresh random data each reset.

## 4. "Try it" button (frontend)

On the login page (`al-rahmah-store-admin/app/login/page.tsx`), add a
secondary button that fills the demo creds and submits — so a prospect is one
tap from a full walkthrough:

```tsx
<button
  type="button"
  onClick={() => { setUsername("demo"); setPassword("demo"); /* then submit */ }}
  className="btn-ghost"
>
  جرّب النظام — دخول تجريبي
</button>
```

(Or link straight to the demo subdomain with the creds pre-noted.) Keep it
visible only on the marketing/demo deployment, not on real stores' logins.

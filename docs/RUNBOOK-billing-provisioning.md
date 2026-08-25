# Runbook — Tenant Provisioning, Billing & Suspension

> **Why P0.** Before you take money you need a repeatable way to (a) stand up a
> new store in minutes, (b) collect monthly, and (c) **cleanly cut off a
> non-paying tenant**. The cut-off is now enforced in code (see §4); this
> runbook is the operational wrapper around it.
>
> Owner: Hamza. Collect in ₪ via cash / bank transfer / PalPay / JawwalPay (no
> Stripe/PayPal friction for this market).

---

## 1. Provision a new tenant (target: < 10 minutes)

Every store is one `Store` row + one owner `User`. Two ways:

### Option A — Django admin (Jazzmin) — the normal path

1. `/admin/` → **Pharmacies → Add**: set `name`, `slug` (lowercase, no spaces — becomes `slug.pharma.ps`), `phone`, `address`, optionally `logo` URL. Leave `is_active = ✓`.
2. **Set the tier** via `enabled_modules` (see §2).
3. `/admin/` → **Users → Add**: username + password, set **store = the new row**, `is_staff` only if they need admin. Leave `allowed_modules` empty (= everything the store has).
4. Send the owner: their URL (`slug.pharma.ps` or the current `alrahmah.clinixa.cloud`), username, and a temporary password (they change it in-app).

### Option B — shell one-liner (scriptable)

```bash
python manage.py shell -c "
from apps.store.models import Store
from django.contrib.auth import get_user_model
U = get_user_model()
ph = Store.objects.create(name='صيدلية X', slug='x-store', phone='09...', enabled_modules=[])  # []=Pro/all
U.objects.create_user('x_owner', password='CHANGE_ME', store=ph)
print('tenant', ph.id, ph.slug)
"
```

Then run the free migration in front of them: **Import → upload their Hesabate
export** (products first, then sales). That single button is your whole
switch-from-Hesabate pitch.

---

## 2. Tiers → modules (this is your pricing, enforced per-tenant)

Modules (keys in `apps/store/modules.py`): `inventory`, `pos`, `price_check`,
`customers`, `debts`, `imports`. **`enabled_modules = []` means ALL six.**

| Tier | ₪/mo | `enabled_modules` value |
|---|---|---|
| **Counter** (أساسي) | 49 | `["inventory","pos","price_check"]` |
| **Pro** ⭐ (احترافي) | 89 | `[]`  *(empty = all six)* |
| **Multi-branch** | 79/branch | `[]` per branch store |

Change a tier any time by editing `enabled_modules` in the admin — the nav and
API access update on the user's next `/me/` fetch (server-enforced by the
`ModuleEnabled` permission, so it's not just cosmetic).

Per-cashier limits: set a user's `allowed_modules` (e.g. `["pos"]` for a cashier
who should only sell). Effective access = store tier ∩ user grant.

---

## 3. Monthly collection

**Cadence:** invoice on the 1st, due by the 7th, grace to the 10th, suspend on
the 11th (see §4). Keep it in a simple sheet (tenant, tier, ₪, paid-through
date, channel).

1. **1st** — send a WhatsApp invoice (amount, tier, "ادفع لـ …", your PalPay/JawwalPay handle or bank/IBAN).
2. On payment — record the **paid-through date**, send a one-line receipt (your own `printReceipt`/a WhatsApp text).
3. **Founding cohort** (first 10–15) — lock their price for life; note it on their row so you never re-quote.

Payment channels for the West Bank: **cash** (on-site, common), **bank
transfer / IBAN**, **PalPay**, **JawwalPay**. Always charge *something* monthly —
recurring is the only path to a stable $1k/mo (Strategy §5).

---

## 4. Suspend on non-payment (enforced in code ✅)

Flipping `Store.is_active = False` now **cleanly locks the whole tenant** —
this was previously a gap (a suspended tenant's staff could still log in).
Enforced in two places, covered by `apps/store/tests/test_billing_suspend.py`:

- **Login is blocked** — `CustomTokenObtainPairSerializer` rejects a suspended tenant's login with a clear Arabic message ("اشتراك الصيدلية موقوف…").
- **Every tenant data request is blocked** — `request_pharmacy_id()` (the single choke-point all scoped reads/writes pass through) raises `403` even for an already-issued, still-valid access token. So you don't have to wait for tokens to expire.

### To suspend

- Admin → **Pharmacies → (tenant) → uncheck `is_active` → Save.** Done. They immediately lose access (data endpoints 403; next login refused).
- Their data is **retained** — nothing is deleted. Reactivating restores full access.

```bash
# scriptable equivalent
python manage.py shell -c "
from apps.store.models import Store
Store.objects.filter(slug='x-store').update(is_active=False)"
```

### To reactivate (payment received)

- Admin → check `is_active` → Save. Instant restore, no data migration.

### Dunning sequence (suggested)

| Day | Action |
|---|---|
| 1 | Invoice (WhatsApp). |
| 7 | Due. |
| 8–10 | Friendly reminder ("تذكير ودّي — فاتورة هذا الشهر"). |
| 11 | **Suspend** (`is_active=False`) + message: "تم إيقاف الحساب مؤقتاً لحين السداد — بياناتك محفوظة." |
| Paid | Reactivate within minutes. |
| +60 days unpaid | Export their data (one-click), archive, then consider deletion per your ToS. |

> Keep suspension **reversible and data-preserving** — the goal is to get paid,
> not to punish. "بياناتك محفوظة" (your data is safe) is what makes the
> suspend-then-pay loop work without losing the customer.

---

## 5. De-provision / offboarding

1. Offer a **one-click data export** first (goodwill + it's in your ToS).
2. Set `is_active=False` (locks access, keeps data).
3. After the retention window in your ToS, delete the `Store` row (cascades to all its data) if they've truly left.

---

## 6. Checklist per new tenant

- [ ] Store row created (name, slug, phone, address, logo).
- [ ] Tier set via `enabled_modules`.
- [ ] Owner user created + credentials delivered.
- [ ] Hesabate data imported in front of them.
- [ ] Added to the billing sheet with paid-through date + locked price if founding.
- [ ] Receipt/label print settings configured on their counter (logo, 58/80mm) — see the app's POS → printer icon.

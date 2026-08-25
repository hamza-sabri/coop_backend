# Runbook — Backups & Disaster Recovery (Neon PITR + Backblaze B2)

> **Why this is P0.** A store losing its ledger is business-ending for them
> and reputation-ending for you. This runbook proves — not assumes — that you
> can bring a tenant's data back. Execute the **Restore Drill (§4) at least once
> before the first paying store**, then quarterly.
>
> Owner: Hamza · Stack: Neon (Postgres) + Backblaze B2 (images) + Dokploy/VPS.
> Facts verified against Neon & Backblaze docs, July 2026 (see Sources).

---

## 0. What we are protecting, and the targets

| Data | Where it lives | Loss = | Backup mechanism |
|---|---|---|---|
| The ledger (sales, debts, customers, meds, users) | **Neon Postgres** | Catastrophic | Neon PITR **+** nightly `pg_dump` to B2 |
| CatalogItem/med images | **Backblaze B2** (private bucket) | Annoying, not fatal | B2 versioning + lifecycle |
| Cart state (in-progress sales) | Postgres + Convex | Minor (ephemeral) | Covered by Postgres backup |

**Recovery targets to commit to (state them in your ToS/SLA):**

- **RPO (max data loss): ≤ 24 h.** With Neon PITR on a paid plan the practical RPO is seconds; the nightly dump caps the worst case at 24 h.
- **RTO (max downtime to restore): ≤ 2 h** for a full rebuild from a dump; **minutes** for a Neon branch restore.

---

## 1. Neon Postgres — confirm Point-in-Time Restore is real

Neon retains a history of WAL changes and lets you restore a branch to any LSN
or timestamp **inside your plan's retention window**. Retention is **plan-gated**:

| Neon plan | History / PITR window | Notes |
|---|---|---|
| **Free** | **up to 6 hours** (≤1 GB of changes) | ⚠️ 6 h is not enough for a business ledger. |
| **Launch** | **up to 7 days** ($0.20/GB-mo) | **Minimum recommended for paying stores.** |
| **Scale** | up to 30 days | For when you have many tenants. |

**Action items**

- [ ] In the Neon Console → your project → **Settings → Storage / History retention**, confirm the window. **If you are on Free, upgrade to Launch and set retention to 7 days before onboarding a paying store.** 6 hours means an error discovered the next morning is unrecoverable via PITR alone.
- [ ] Confirm which branch is **production** (the root branch — only **root** branches support instant restore; child branches do not).
- [ ] Note the project ID, branch name, and the connection string source (it's in `DATABASE_URL` on the VPS / Dokploy env, pointed at the `-pooler` host).

---

## 2. Nightly logical backup to B2 (independent of Neon)

PITR protects against *"undo the last N days."* It does **not** protect against
*"my Neon account/project is gone."* A nightly `pg_dump` shipped to B2 is the
cheap insurance that survives Neon itself. It also gives you a portable file you
can restore **anywhere** (another Neon project, a local Postgres, a different
host).

Install once on the VPS (or as a Dokploy scheduled job / cron):

```bash
# /opt/pharma/backup-db.sh   —  chmod +x, run nightly via cron
set -euo pipefail

STAMP="$(date +%F_%H%M)"
OUT="/tmp/pharma_${STAMP}.sql.gz"

# 1) Dump the whole database (schema + data), compressed.
#    DATABASE_URL is the same Neon pooled URL the app uses.
pg_dump "$DATABASE_URL" --no-owner --no-privileges | gzip > "$OUT"

# 2) Push to a SEPARATE B2 bucket used ONLY for DB dumps (not the images bucket).
#    Uses the B2 S3-compatible endpoint; aws-cli or b2 cli both work.
aws s3 cp "$OUT" "s3://${B2_DB_BUCKET}/db/${STAMP}.sql.gz" \
  --endpoint-url "$B2_ENDPOINT_URL"

# 3) Keep local disk clean.
rm -f "$OUT"
echo "backup ok: ${STAMP}"
```

Cron (03:30 every night, Palestine time — set the server TZ or add `CRON_TZ`):

```cron
CRON_TZ=Asia/Hebron
30 3 * * *  /opt/pharma/backup-db.sh >> /var/log/pharma-backup.log 2>&1
```

**Action items**

- [ ] Create a dedicated **`pharma-db-backups`** B2 bucket (private), separate from the images bucket.
- [ ] Add a `B2_DB_BUCKET` + reuse `B2_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID/SECRET` (the B2 key) to the script's environment.
- [ ] Add the cron job; confirm `/var/log/pharma-backup.log` shows `backup ok` tomorrow morning.
- [ ] Put a **B2 lifecycle rule** on `pharma-db-backups`: keep dumps 30–90 days, then expire (see §3).

---

## 3. Backblaze B2 — confirm versioning + lifecycle

B2 buckets are **always versioned by default**: deleting a file by name only
hides the latest version; older versions remain and are downloadable by File ID.
**But fully-deleted versions cannot be restored by Backblaze** — so the safety
net is *retention*, controlled by **Lifecycle Rules**.

**Action items (images bucket)**

- [ ] Confirm the images bucket is **Private** (it is in code — `AWS_DEFAULT_ACL=private`, `AWS_QUERYSTRING_AUTH=True`, signed URLs expire in `B2_URL_EXPIRE`, default 7 days).
- [ ] Set a lifecycle rule to **Keep Prior Versions for N days** (e.g. 30) rather than "Keep Only Last Version" — that way an accidental overwrite/delete is recoverable for 30 days, then old versions expire so storage doesn't balloon.
- [ ] (Optional, strong) Enable **Object Lock** (governance mode) on the DB-backups bucket so a compromised key can't wipe your backups — ransomware/insider protection.

**Action items (DB-backups bucket)**

- [ ] Lifecycle: keep 30–90 days of nightly dumps, then expire.

---

## 4. THE RESTORE DRILL — do this before you sell (and quarterly)

A backup you have never restored is a rumor. Two drills; do **both** once, then
Drill B quarterly.

### Drill A — Neon Branch Restore (fast path, "undo an oops")

Simulates: someone deleted/overwrote data and you catch it same-day.

1. In Neon Console, note the **current time** and run a harmless known change on a **staging** branch (e.g. insert a customer named `RESTORE-TEST`).
2. Wait 2 minutes, then delete it.
3. Neon Console → **Branches → (branch) → Restore** → choose **"Restore to timestamp"** just before the delete. Confirm Neon warns it will save the current state as a backup branch.
4. ✅ Pass = `RESTORE-TEST` is back. Note how long it took (should be seconds–minutes). Record the steps with screenshots for the real incident.

> Reminder: only **root** branches can be restored this way. Restore saves the pre-restore state as a new branch, so a mistaken restore is itself reversible.

### Drill B — Full rebuild from the B2 dump (the "Neon is gone" path)

Simulates: total loss of the Neon project. This is the drill that actually
proves the business can survive.

```bash
# 1) Pull last night's dump from B2.
aws s3 cp "s3://${B2_DB_BUCKET}/db/<LATEST>.sql.gz" /tmp/restore.sql.gz \
  --endpoint-url "$B2_ENDPOINT_URL"

# 2) Create a THROWAWAY target: a new empty Neon project/branch, or a local
#    Postgres container. Get its connection string as RESTORE_URL.
#    (Local example:)
docker run -d --name pg-restore -e POSTGRES_PASSWORD=x -p 5433:5432 postgres:16
RESTORE_URL="postgres://postgres:x@localhost:5433/postgres"

# 3) Restore into it.
gunzip -c /tmp/restore.sql.gz | psql "$RESTORE_URL"

# 4) Verify row counts against production expectations.
psql "$RESTORE_URL" -c "SELECT
   (SELECT count(*) FROM pharmacy_sale)      AS sales,
   (SELECT count(*) FROM pharmacy_debt)      AS debts,
   (SELECT count(*) FROM pharmacy_customer)  AS customers,
   (SELECT count(*) FROM pharmacy_medication) AS meds,
   (SELECT count(*) FROM pharmacy_pharmacy)  AS tenants;"

# 5) Point a scratch copy of the app at RESTORE_URL and log in as a test tenant
#    to confirm the data is coherent (a sale opens, a debt shows, an image loads).

# 6) Tear down the throwaway target.
docker rm -f pg-restore
```

✅ **Pass criteria:** row counts are within one day of production, the app boots
against the restored DB, and a tenant's sales/debts render correctly. Write the
elapsed time into the RTO line in §0.

---

## 5. Restore-readiness checklist (print this)

- [ ] Neon retention window ≥ 7 days (Launch+), confirmed in console.
- [ ] Nightly `pg_dump` → B2 running; last log line is `backup ok` (checked within 24 h).
- [ ] B2 images bucket: private, signed URLs, lifecycle keeps prior versions ≥ 30 days.
- [ ] B2 DB-backups bucket: separate, lifecycle 30–90 days, (optional) Object Lock.
- [ ] **Drill A (Neon branch restore)** passed and screenshotted.
- [ ] **Drill B (full rebuild from dump)** passed; RTO recorded.
- [ ] Neon + Backblaze credentials stored somewhere you can reach if the VPS is down (a password manager, not only on the VPS).
- [ ] One-page "if the DB is lost" incident card kept with the credentials (who to call at Neon/Backblaze, the two restore procedures above).

---

## 6. Incident quick-reference (the 2am version)

1. **Data corrupted/deleted today** → Neon Console → Branch Restore to a timestamp before the incident (Drill A). Fastest.
2. **Neon project/account lost** → provision a new Neon project, restore last night's B2 dump (Drill B), repoint `DATABASE_URL` in Dokploy, redeploy. Max ~24 h of data lost (last night's dump).
3. **Image missing/overwritten** → B2 → find the file → restore the prior version by File ID (available because versioning + lifecycle keep it).
4. Always **communicate** to the affected store proactively; your handling of an incident is itself the reputation event.

---

### Sources
- Neon — Point-in-Time Restore / Branch Restore & history retention: https://neon.com/docs/guides/backup-restore , https://neon.com/docs/introduction/branch-restore , https://neon.com/docs/introduction/plans
- Backblaze B2 — versioning & lifecycle rules: https://www.backblaze.com/docs/cloud-storage-lifecycle-rules , https://www.backblaze.com/docs/cloud-storage-file-versions

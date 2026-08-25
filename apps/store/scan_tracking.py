"""Anonymous price-check scan tracking (the public /price kiosk).

The customer's browser fires a silent, fire-and-forget beacon after each scan
(`POST /api/v1/public/scan-log/`) — it never blocks the price lookup, never
retries, and shows the shopper nothing. The endpoint just bumps a per-day Redis
counter (one HINCRBY). Once a day `manage.py flush_scan_counters` (a Dokploy
cron at 01:00) folds those counters into the durable ``ScanDaily`` table.

Nothing here can slow or break a scan: every Redis call is wrapped and
best-effort — a Redis hiccup means one uncounted scan, never a user-visible
error.

Redis keys (raw client — NOT the Django cache key-prefix; the flush uses the
same raw client so names line up). They are deliberately NOT namespaced by the
catalogue version, so a price edit or redeploy never scatters a day's counts:

    scan:cnt:{pid}:{YYYY-MM-DD}    HASH  barcode -> count
    scan:meta:{pid}:{YYYY-MM-DD}   HASH  barcode -> "<found 0/1>|<name>"

Both carry a multi-day TTL, so a missed nightly run self-heals on the next one.
"""
from __future__ import annotations

import logging
from datetime import date

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

# A day's counters live this long in Redis before it reclaims them — comfortably
# longer than the gap between nightly flushes so one skipped run self-heals.
COUNTER_TTL = 60 * 60 * 24 * 3  # 3 days
_SEP = "|"


def cnt_key(pid, day):
    return f"scan:cnt:{pid}:{day}"


def meta_key(pid, day):
    return f"scan:meta:{pid}:{day}"


def _redis():
    """Raw redis client, or None when Redis isn't configured (local-mem dev).

    We use the raw client (not the Django cache API) for atomic HINCRBY and to
    enumerate a day's fields at flush time. Never raises — callers degrade to a
    no-op so analytics can't break a scan.
    """
    if not getattr(settings, "REDIS_ENABLED", False):
        return None
    try:
        from django_redis import get_redis_connection

        return get_redis_connection("default")
    except Exception:  # pragma: no cover - defensive
        return None


def _as_str(v):
    return v.decode("utf-8", "ignore") if isinstance(v, bytes) else v


def _pack_meta(found, name):
    return f"{1 if found else 0}{_SEP}{(name or '')[:255]}"


def _unpack_meta(raw):
    if raw is None:
        return (False, "")
    raw = _as_str(raw)
    flag, _, name = raw.partition(_SEP)
    return (flag == "1", name)


def record_scan(pid, barcode, *, found=False, name=""):
    """Count one customer barcode scan — best-effort, never raises.

    Hot path is a single HINCRBY; on the first sight of a barcode that day it
    also stamps the found/name meta and the TTLs (a handful of extra micro-ops,
    once per barcode per day).
    """
    if not pid or not barcode:
        return
    client = _redis()
    if client is None:
        return
    try:
        day = timezone.localdate().isoformat()
        ck = cnt_key(pid, day)
        first = client.hincrby(ck, str(barcode), 1) == 1
        if first:
            mk = meta_key(pid, day)
            client.hset(mk, str(barcode), _pack_meta(found, name))
            client.expire(ck, COUNTER_TTL)
            client.expire(mk, COUNTER_TTL)
    except Exception:  # pragma: no cover - analytics must never break a scan
        return


def clear_pharmacy(pid):
    """Delete a store's live Redis scan counters (count + meta) so a reset
    from the report page isn't re-populated on the next flush. Best-effort and
    scoped strictly to this store's keys."""
    if not pid:
        return
    client = _redis()
    if client is None:
        return
    try:
        keys = list(client.scan_iter(match=f"scan:cnt:{pid}:*", count=200))
        keys += list(client.scan_iter(match=f"scan:meta:{pid}:*", count=200))
        if keys:
            client.delete(*keys)
    except Exception:  # pragma: no cover - defensive
        return


def _upsert_add(models, pid, day, barcode, delta, found, med_id, name):
    """INCREMENT (store, day, barcode).count by `delta`, creating the row if
    absent. Because Redis is zeroed on every flush, each drained batch is a
    DELTA — so we add, never overwrite, and a barcode scanned across several
    flushes on one day sums correctly. The single `UPDATE … count = count +
    delta` is atomic in Postgres; the create path retries on a unique clash."""
    qs = models.ScanDaily.objects.unscoped()
    fields = dict(
        found=found, product_id=med_id, medication_name=name
    )
    if qs.filter(store_id=pid, day=day, barcode=barcode).update(
        count=F("count") + delta, **fields
    ):
        return
    try:
        qs.create(store_id=pid, day=day, barcode=barcode, count=delta, **fields)
    except IntegrityError:  # concurrent create — fall back to increment
        qs.filter(store_id=pid, day=day, barcode=barcode).update(
            count=F("count") + delta, **fields
        )


def _flush_day(models, pid, day, counts, metas):
    """ADD one drained batch of a store-day's counters into ScanDaily.

    found / product / name are resolved fresh each time: a barcode matching a
    product (by exact barcode) links to it and counts as found; otherwise the
    scan-time meta flag decides (captures variant / alt-barcode hits)."""
    barcodes = [_as_str(b) for b in counts.keys()]
    # Resolve barcode -> live product in ONE query so matched rows link to the
    # product for repricing / reordering. Unmatched barcodes stay a demand
    # signal.
    med_by_barcode = {}
    try:
        for m in (
            models.Product.objects.for_pharmacy(pid)
            .filter(barcode__in=barcodes)
            .values("id", "name", "barcode")
        ):
            med_by_barcode[m["barcode"]] = (m["id"], m["name"])
    except Exception:  # pragma: no cover - defensive
        med_by_barcode = {}

    written = 0
    with transaction.atomic():
        for b_raw, c_raw in counts.items():
            barcode = _as_str(b_raw)
            try:
                delta = int(c_raw)
            except (TypeError, ValueError):
                continue
            if delta == 0:
                continue
            meta_found, meta_name = _unpack_meta(
                metas.get(b_raw) if metas else None
            )
            hit = med_by_barcode.get(barcode)
            if hit:
                med_id, med_name = hit
                found = True
                name = meta_name or med_name
            else:
                med_id = None
                found = meta_found  # variant / alt-barcode hit (client-reported)
                name = meta_name
            _upsert_add(
                models, pid, day, barcode[:120], delta, found, med_id,
                (name or "")[:255],
            )
            written += 1
    return written


def flush(write=None):
    """Drain EVERY store-day's Redis scan counters into ScanDaily and reset
    the Redis counters to zero.

    Every run — the nightly 01:00 cron or a manual trigger — folds whatever is
    currently in Redis (all days, including today) into the DB and clears it, so
    the counters always start fresh. Two guarantees keep the analysis clean:

      • Atomic capture + clear: for each store-day the count and meta hashes
        are read AND deleted inside one Redis transaction (MULTI/EXEC). A scan
        landing mid-flush is therefore either fully counted in this batch or
        starts the next (zeroed) counter — never lost, never double-counted.
      • Accumulating write: because Redis is zeroed each time, the DB row is
        INCREMENTED by the drained delta (see `_upsert_add`), so repeated
        same-day flushes sum to the true total.

    If the DB write fails after Redis was cleared, the batch is re-injected into
    Redis so the next run retries (best-effort; nothing silently lost).
    """
    client = _redis()
    if client is None:
        if write:
            write("Redis not configured — nothing to flush.")
        return {"days": 0, "rows": 0}

    from . import models  # lazy: avoid import-time app-loading issues

    days_done = rows = 0
    # Snapshot the key list first — we mutate (delete) keys as we go.
    for raw_key in list(client.scan_iter(match="scan:cnt:*", count=200)):
        key = _as_str(raw_key)
        parts = key.split(":")
        if len(parts) != 4:
            continue
        _, _, pid_s, day_s = parts
        try:
            day = date.fromisoformat(day_s)
            pid = int(pid_s)
        except ValueError:
            continue
        mkey = meta_key(pid, day_s)

        # Atomic capture + clear (MULTI/EXEC): read both hashes and delete them
        # as one indivisible unit.
        try:
            pipe = client.pipeline()  # transactional by default
            pipe.hgetall(key)
            pipe.hgetall(mkey)
            pipe.delete(key)
            pipe.delete(mkey)
            counts, metas, _, _ = pipe.execute()
        except Exception:
            logger.exception("flush_scan_counters: capture failed on %s", key)
            if write:
                write(f"  ! skipped {key} (capture failed)")
            continue

        if not counts:
            continue
        try:
            rows += _flush_day(models, pid, day, counts, metas)
            days_done += 1
        except Exception:
            # DB write failed AFTER Redis was cleared — put the counts back so
            # the next run retries. Scans that arrived meanwhile just add on top.
            logger.exception(
                "flush_scan_counters: DB write failed on %s; re-injecting", key
            )
            try:
                p2 = client.pipeline(transaction=False)
                for b, c in counts.items():
                    p2.hincrby(key, b, int(c))
                for b, m in (metas or {}).items():
                    p2.hsetnx(mkey, b, m)
                p2.execute()
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "flush_scan_counters: re-inject failed on %s (batch lost)", key
                )
            if write:
                write(f"  ! DB write failed on {key} — restored to Redis for retry")
            continue

    if write:
        write(f"Flushed {rows} scan rows across {days_done} store-days (Redis drained).")
    return {"days": days_done, "rows": rows}

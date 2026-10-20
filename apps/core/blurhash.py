"""Blurhash: a ~28-character code for a blurred preview of a picture, shown
while the real picture loads (https://blurha.sh).

How it reaches the browser: every picture URL the API hands out carries its
code in the URL fragment, ``…/abc.jpg#bh=<code>``. Browsers never send the
fragment and ignore it for caching, so the picture loads exactly as before;
the frontend's <BlurImg> reads it and paints the blur until the picture
arrives. No response shape changes, no extra field to thread through.

Where codes come from:
- an upload is hashed on the spot (the file is already in memory);
- any other picture (catalogue photos, pasted URLs, synced items) is hashed
  once in the background the first time it is served;
- ``manage.py blurhash_images`` fills in everything already stored, once.

Codes live in ImageHash, keyed by the picture's identity (its storage key, or
the URL without query/fragment), and are kept in memory per process: serving
a code costs a dict lookup, never a query. Nothing here may ever fail a
request or a save — every path swallows its own errors.
"""
from __future__ import annotations

import hashlib
import io
import logging
import math
import queue
import re
import threading
import time
from urllib.parse import quote, unquote, urlsplit

from django.conf import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Encoder (the reference algorithm, https://github.com/woltapp/blurhash)

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~"


def _b83(value: int, length: int) -> str:
    return "".join(_CHARS[(value // 83 ** (length - i)) % 83] for i in range(1, length + 1))


def _to_linear(v: int) -> float:
    v = v / 255
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _to_srgb(v: float) -> int:
    v = max(0.0, min(1.0, v))
    return int(v * 12.92 * 255 + 0.5) if v <= 0.0031308 else int((1.055 * v ** (1 / 2.4) - 0.055) * 255 + 0.5)


def _sign_pow(v: float, e: float) -> float:
    return math.copysign(abs(v) ** e, v)


def encode_pixels(pixels: list[tuple[int, int, int]], width: int, height: int, cx: int = 4, cy: int = 3) -> str:
    lin = [(_to_linear(r), _to_linear(g), _to_linear(b)) for r, g, b in pixels]
    cos_x = [[math.cos(math.pi * i * x / width) for x in range(width)] for i in range(cx)]
    cos_y = [[math.cos(math.pi * j * y / height) for y in range(height)] for j in range(cy)]
    factors = []
    for j in range(cy):
        for i in range(cx):
            norm = 1 if i == 0 and j == 0 else 2
            r = g = b = 0.0
            for y in range(height):
                row = y * width
                fy = cos_y[j][y]
                for x in range(width):
                    basis = cos_x[i][x] * fy
                    pr, pg, pb = lin[row + x]
                    r += basis * pr
                    g += basis * pg
                    b += basis * pb
            scale = norm / (width * height)
            factors.append((r * scale, g * scale, b * scale))

    dc, ac = factors[0], factors[1:]
    out = _b83((cx - 1) + (cy - 1) * 9, 1)
    if ac:
        actual_max = max(abs(c) for f in ac for c in f)
        q_max = max(0, min(82, int(actual_max * 166 - 0.5)))
        max_value = (q_max + 1) / 166
        out += _b83(q_max, 1)
    else:
        max_value = 1
        out += _b83(0, 1)
    out += _b83((_to_srgb(dc[0]) << 16) + (_to_srgb(dc[1]) << 8) + _to_srgb(dc[2]), 4)
    for r, g, b in ac:
        q = [max(0, min(18, int(math.floor(_sign_pow(c / max_value, 0.5) * 9 + 9.5)))) for c in (r, g, b)]
        out += _b83(q[0] * 19 * 19 + q[1] * 19 + q[2], 2)
    return out


def hash_bytes(data: bytes) -> str | None:
    """Blurhash of an image file's bytes, or None if it is not a readable image."""
    try:
        from PIL import Image, ImageOps

        im = Image.open(io.BytesIO(data))
        # JPEGs decode straight at a fraction of their size: a 4000px photo
        # costs about what a 500px one does.
        try:
            im.draft("RGB", (64, 64))
        except Exception:
            pass
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im)
            im = bg
        im = im.convert("RGB")
        im.thumbnail((32, 32))
        w, h = im.size
        if w < 1 or h < 1:
            return None
        # Landscape/portrait pictures get more components along their long side.
        cx, cy = (4, 3) if w >= h else (3, 4)
        return encode_pixels(list(im.getdata()), w, h, cx, cy)
    except Exception as exc:  # noqa: BLE001
        log.info("blurhash: unreadable image (%s)", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# Identity: the same picture must map to one code whether we see it as
# "b2://products/<uuid>.jpg", its signed URL, or "/media/products/<uuid>.jpg".

_FRAG = "#bh="
#: Not pictures (or not ones Pillow reads): never fetched for hashing.
_NOT_PICTURE = re.compile(r"\.(mp4|mov|webm|m4v|avi|mkv|mp3|pdf|svg|html?)$", re.I)
_UUID_FILE = re.compile(r"[0-9a-f]{32}\.[a-z0-9]{2,5}$", re.I)


def strip(value):
    """Drop a "#bh=…" fragment (clients echo URLs back)."""
    if isinstance(value, str) and _FRAG in value:
        return value.split(_FRAG, 1)[0]
    return value


def identity(value: str) -> str | None:
    if not isinstance(value, str) or not value or value.startswith("data:"):
        return None
    value = strip(value)
    from apps.core.uploads import B2_SCHEME

    if value.startswith(B2_SCHEME):
        path = value[len(B2_SCHEME):]
    else:
        try:
            parts = urlsplit(value)
        except ValueError:
            return None
        if parts.scheme not in ("http", "https", "") or not parts.path:
            return None
        path = unquote(parts.path)
        if not parts.scheme and not path.startswith("/media/"):
            return None
        # Our own uploads are named <uuid>.<ext>: that name alone identifies
        # them, whatever host or signature they are served under.
        name = path.rsplit("/", 1)[-1]
        if not _UUID_FILE.search(name):
            return "url:" + hashlib.sha1(f"{parts.netloc}{path}".encode()).hexdigest()
    name = path.rsplit("/", 1)[-1]
    if _UUID_FILE.search(name):
        return "f:" + name.lower()
    return "url:" + hashlib.sha1(path.encode()).hexdigest()


# ---------------------------------------------------------------------------
# In-memory table, refreshed from the database every few minutes.

_lock = threading.Lock()
_memo: dict[str, str] = {}
_loaded_at = 0.0
_RELOAD = 300


def _ensure_loaded():
    global _loaded_at
    now = time.monotonic()
    if now - _loaded_at < _RELOAD:
        return
    _loaded_at = now  # set first: a failing database is not retried per call
    try:
        from apps.core.models import ImageHash

        rows = dict(ImageHash.objects.values_list("key", "blurhash"))
        with _lock:
            _memo.update(rows)
    except Exception as exc:  # noqa: BLE001
        log.info("blurhash: could not load codes (%s)", type(exc).__name__)


def lookup(value) -> str | None:
    key = identity(value)
    if not key:
        return None
    _ensure_loaded()
    return _memo.get(key)


def remember(value, code: str | None) -> None:
    key = identity(value)
    if not key or not code:
        return
    with _lock:
        _memo[key] = code
    try:
        from apps.core.models import ImageHash

        ImageHash.objects.update_or_create(key=key, defaults={"blurhash": code})
    except Exception as exc:  # noqa: BLE001
        log.info("blurhash: could not save code (%s)", type(exc).__name__)


def reset_memory():
    """Tests only."""
    global _loaded_at
    with _lock:
        _memo.clear()
    _loaded_at = 0.0
    _pending.clear()


# ---------------------------------------------------------------------------
# Serving: append the code to a URL.

def with_hash(url, stored=None):
    """`url` with "#bh=<code>" when a code is known for it (or for `stored`).
    Unknown pictures are queued to be hashed in the background."""
    if not isinstance(url, str) or not url or _FRAG in url or url.startswith("data:"):
        return url
    try:
        code = lookup(stored or url)
        if code:
            return f"{url}{_FRAG}{quote(code, safe='')}"
        enqueue(stored or url)
    except Exception:  # noqa: BLE001
        pass
    return url


# ---------------------------------------------------------------------------
# Fetching a picture's bytes, wherever it lives.

MAX_BYTES = 8 * 1024 * 1024


def fetch_bytes(value: str) -> bytes | None:
    from django.core.files.storage import default_storage

    from apps.core.uploads import B2_SCHEME

    value = strip(value)
    try:
        if value.startswith(B2_SCHEME):
            with default_storage.open(value[len(B2_SCHEME):]) as f:
                return f.read(MAX_BYTES)
        if value.startswith(("http://", "https://")):
            import requests

            with requests.get(value, timeout=10, stream=True) as r:
                if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
                    return None
                data = r.raw.read(MAX_BYTES + 1, decode_content=True)
                return None if len(data) > MAX_BYTES else data
        if "/media/" in value:
            rel = value.split("/media/", 1)[1]
            with default_storage.open(rel) as f:
                return f.read(MAX_BYTES)
    except Exception as exc:  # noqa: BLE001
        log.info("blurhash: could not fetch image (%s)", type(exc).__name__)
    return None


def compute(value: str) -> str | None:
    """Fetch, hash and store one picture. Returns the code (or None)."""
    data = fetch_bytes(value)
    code = hash_bytes(data) if data else None
    if code:
        remember(value, code)
    return code


# ---------------------------------------------------------------------------
# Background hashing: one low-priority thread per process, one picture at a
# time, a bounded queue, and each picture tried at most once per process.

_queue: "queue.Queue[str]" = queue.Queue(maxsize=500)
_pending: set[str] = set()
_worker: threading.Thread | None = None


def enqueue(value) -> None:
    if not getattr(settings, "BLURHASH_BACKGROUND", True):
        return
    key = identity(value)
    if not key or key in _pending:
        return
    from apps.core.uploads import B2_SCHEME

    v = strip(value)
    if not (v.startswith((B2_SCHEME, "http://", "https://")) or "/media/" in v):
        return
    if _NOT_PICTURE.search(v.split("?", 1)[0]):
        return
    # A signed URL expires; prefer it anyway — it is what we can fetch now.
    try:
        _queue.put_nowait(v)
    except queue.Full:
        return
    _pending.add(key)
    _start_worker()


def _start_worker():
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_work, name="blurhash", daemon=True)
        _worker.start()


def _work():
    from django.db import close_old_connections

    while True:
        value = _queue.get()
        try:
            if not lookup(value):
                compute(value)
        except Exception:  # noqa: BLE001
            pass
        finally:
            close_old_connections()
        time.sleep(0.2)  # stay out of the way of real requests


# ---------------------------------------------------------------------------
# Picture fields across the models (for the strip-on-save hook and the
# backfill command).

_PICTURE_FIELD = re.compile(r"image|logo|avatar|photo|picture|thumb", re.I)
_fields_cache: dict[type, tuple[str, ...]] = {}


def picture_fields(model) -> tuple[str, ...]:
    if model not in _fields_cache:
        from django.db import models as m

        _fields_cache[model] = tuple(
            f.attname
            for f in model._meta.concrete_fields
            if isinstance(f, (m.CharField, m.TextField))  # URLField is a CharField
            and _PICTURE_FIELD.search(f.name)
        )
    return _fields_cache[model]


def strip_picture_fields(sender, instance, **kwargs):
    try:
        for name in picture_fields(sender):
            v = getattr(instance, name, None)
            if isinstance(v, str) and _FRAG in v:
                setattr(instance, name, strip(v))
    except Exception:  # noqa: BLE001
        pass

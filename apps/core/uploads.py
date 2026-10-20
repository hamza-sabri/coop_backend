"""
Turn an uploaded file into a stored URL, against whatever storage is active.

When Backblaze B2 is configured (B2_* env vars) files land on B2 via
django-storages and get clean public URLs; otherwise they land on the local
filesystem under /media. Callers don't need to know which — that's the point,
and it's why the same "upload a file, get a URL" API works before and after B2
is wired up.
"""
import os
import uuid

from django.conf import settings
from django.core.files.storage import default_storage

#: Marker scheme for files living on B2. The DB keeps the stable storage key
#: ("b2://products/abc.jpg"); a fresh signed URL is generated on every read
#: so PRIVATE (free) buckets work — no public-bucket fee, no expiring links in
#: the database. Signing is local crypto: zero extra requests.
B2_SCHEME = "b2://"


def store_upload(file, folder: str = "uploads", request=None) -> str:
    """Save `file` to the active storage backend and return its URL.

    For local storage the URL is relative (``/media/...``); when a DRF request
    is passed we make it absolute so it validates as a proper URL. On B2 the URL
    is already absolute and public.
    """
    original = getattr(file, "name", "") or ""
    ext = os.path.splitext(original)[1].lower()
    key = f"{folder.strip('/')}/{uuid.uuid4().hex}{ext}"
    data = _bytes_of(file)
    saved_name = default_storage.save(key, file)
    if getattr(settings, "STORAGE_ENABLED", False):
        stored = f"{B2_SCHEME}{saved_name}"
    else:
        stored = default_storage.url(saved_name)
        if request is not None and stored.startswith("/"):
            stored = request.build_absolute_uri(stored)
    if data:
        # Blur code + small copy now, while the file is in memory.
        try:
            from apps.core import blurhash

            blurhash.process(stored, data)
        except Exception:  # noqa: BLE001 — never fails an upload
            pass
    return stored


def _bytes_of(file) -> bytes | None:
    try:
        from apps.core import blurhash

        size = getattr(file, "size", None)
        if size is not None and size > blurhash.MAX_BYTES:
            return None
        pos = file.tell() if hasattr(file, "tell") else 0
        data = file.read()
        file.seek(pos)
        return data
    except Exception:  # noqa: BLE001
        try:
            file.seek(0)
        except Exception:  # noqa: BLE001
            pass
        return None


#: How long one signed link is handed out before a new one is made. Well
#: inside the link's own lifetime (B2_URL_EXPIRE, 7 days), so a link given out
#: at the end of the window still works for days.
SIGNED_URL_REUSE = 24 * 3600


def resolve_stored_url(value) -> str:
    """Turn a stored value into a servable URL.

    ``b2://<key>`` → a signed B2 URL (valid B2_URL_EXPIRE seconds);
    anything else (external URLs, local /media paths) passes through.

    The SAME signed link is reused for a day. Signing afresh on every response
    gave every picture a new address each time the app refreshed its data, so
    the browser could never use its saved copy and downloaded every picture
    again and again — enough to exhaust Backblaze's daily download cap and
    break every picture in the shop until the next day.
    """
    from apps.core import blurhash

    value = blurhash.strip(value)
    return blurhash.with_hash(_resolve(value), value)


def _resolve(value) -> str:
    if isinstance(value, str) and value.startswith(B2_SCHEME):
        key = value[len(B2_SCHEME):]
        from django.core.cache import cache

        ck = f"b2url:{key}"
        try:
            url = cache.get(ck)
        except Exception:
            url = None
        if url:
            return url
        try:
            url = default_storage.url(key)
        except Exception:
            return ""
        try:
            cache.set(ck, url, SIGNED_URL_REUSE)
        except Exception:
            pass
        return url
    return value or ""

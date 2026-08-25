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
    saved_name = default_storage.save(key, file)
    if getattr(settings, "STORAGE_ENABLED", False):
        return f"{B2_SCHEME}{saved_name}"
    url = default_storage.url(saved_name)
    if request is not None and url.startswith("/"):
        url = request.build_absolute_uri(url)
    return url


def resolve_stored_url(value) -> str:
    """Turn a stored value into a servable URL.

    ``b2://<key>`` → freshly signed B2 URL (valid B2_URL_EXPIRE seconds);
    anything else (external URLs, local /media paths) passes through.
    """
    if isinstance(value, str) and value.startswith(B2_SCHEME):
        try:
            return default_storage.url(value[len(B2_SCHEME):])
        except Exception:
            return ""
    return value or ""

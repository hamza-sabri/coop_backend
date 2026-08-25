"""
Storage helpers that work with whatever DEFAULT storage is active.

When Backblaze B2 is configured (B2_* env vars) these operate against B2 via
django-storages; otherwise they operate against the local filesystem. Callers
don't need to know which — that's the point.
"""
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


def save_bytes(path: str, data: bytes, overwrite: bool = False) -> str:
    """Save raw bytes and return the stored name/path."""
    if overwrite and default_storage.exists(path):
        default_storage.delete(path)
    return default_storage.save(path, ContentFile(data))


def file_url(name: str) -> str:
    """Public/served URL for a stored file."""
    return default_storage.url(name)


def delete_file(name: str) -> None:
    if name and default_storage.exists(name):
        default_storage.delete(name)


def presigned_url(name: str, expires: int = 3600):
    """Return a time-limited signed URL when the backend supports it (B2/S3).

    Falls back to the plain URL for the local filesystem backend.
    """
    try:
        # S3Boto3Storage exposes url(..., expire=...) and a connection object.
        return default_storage.url(name, expire=expires)  # type: ignore[call-arg]
    except TypeError:
        return default_storage.url(name)

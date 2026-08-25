"""
config package.

Expose the Celery app so shared_task can find it, but only if Celery is
installed. The project runs perfectly fine without Celery/Redis configured.
"""
try:
    from .celery import app as celery_app  # noqa: F401

    __all__ = ("celery_app",)
except Exception:  # pragma: no cover - Celery optional
    # Celery not installed or misconfigured: the backend still works.
    __all__ = ()

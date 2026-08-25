"""
Thin, safe wrappers around Sentry.

Sentry is initialised in settings.py only when SENTRY_DSN is set. These helpers
are always importable and turn into no-ops when Sentry isn't active, so app
code can call them unconditionally.
"""


def capture_exception(exc: BaseException) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def capture_message(message: str, level: str = "info") -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_message(message, level=level)
    except Exception:
        pass

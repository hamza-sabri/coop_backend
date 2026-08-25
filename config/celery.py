"""
Optional Celery application.

If Celery isn't installed or no broker URL is configured, importing this
module fails gracefully (see config/__init__.py) and the rest of the app is
unaffected. Tasks defined with @shared_task will simply run eagerly / not at
all until a worker and broker are present.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Broker/result backend fall back to REDIS_URL, then to a local default.
_broker = (
    os.getenv("CELERY_BROKER_URL")
    or os.getenv("REDIS_URL")
    or "redis://127.0.0.1:6379/0"
)

app = Celery("config", broker=_broker, backend=_broker)

# Read any CELERY_* settings from Django settings.
app.config_from_object("django.conf:settings", namespace="CELERY")

# If no real broker is reachable, run tasks eagerly so calling .delay() never
# blocks a request. Flip CELERY_TASK_ALWAYS_EAGER off in prod once a worker
# exists.
app.conf.task_always_eager = os.getenv("CELERY_TASK_ALWAYS_EAGER", "").lower() in (
    "1",
    "true",
    "yes",
)

# Auto-discover tasks.py in every installed app.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):  # pragma: no cover
    print(f"Request: {self.request!r}")

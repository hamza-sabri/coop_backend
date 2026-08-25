#!/bin/sh
# =============================================================================
# Container entrypoint — runs before the app serves traffic.
# Requires DATABASE_URL (and SECRET_KEY) to be set in the environment.
# =============================================================================
set -e

echo "▶ Running migrations..."
python manage.py migrate --noinput

echo "▶ Collecting static files..."
python manage.py collectstatic --noinput

echo "▶ Starting gunicorn..."
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-3}" \
    --worker-class gthread \
    --threads "${GUNICORN_THREADS:-6}" \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -

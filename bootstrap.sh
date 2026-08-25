#!/usr/bin/env bash
# =============================================================================
# AL-Rahmah — one-shot local setup.
# Creates the venv, installs deps, migrates the Neon DB (from .env), imports the
# ~21k meds, and makes an admin login. Safe to re-run (idempotent).
#
#   ./bootstrap.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "▶ Virtualenv…"
[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "▶ Dependencies…"
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "▶ Migrating (Neon, from .env)…"
python manage.py migrate

echo "▶ Importing products (data/price-list.xlsx → Neon)…"
python manage.py import_meds

echo "▶ Admin login…"
if python manage.py shell -c "import sys; from django.contrib.auth import get_user_model as g; sys.exit(0 if g().objects.filter(is_superuser=True).exists() else 1)"; then
  echo "  A superuser already exists — skipping createsuperuser."
else
  python manage.py createsuperuser
fi

echo
echo "✓ Done."
echo "  Run:   python manage.py runserver"
echo "  Docs:  http://127.0.0.1:8000/api/docs/"
echo "  Admin: http://127.0.0.1:8000/admin/"

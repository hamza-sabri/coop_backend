web: gunicorn config.wsgi --log-file -
release: python manage.py migrate --noinput
worker: celery -A config worker -l info

#!/bin/bash
set -e

echo "Applying database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Supervisor (Daphne + Pollers)..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf

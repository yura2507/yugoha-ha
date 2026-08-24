#!/usr/bin/with-contenv bashio
set -e

mkdir -p /data

# One-install mode:
# copy/update the bundled Home Assistant integration, register Supervisor
# discovery and restart Core only when the integration version changed.
/opt/yugoha-venv/bin/python /app/bootstrap.py || true

exec /opt/yugoha-venv/bin/gunicorn   --bind 0.0.0.0:8099   --workers 1   --threads 8   --timeout 60   --access-logfile -   --error-logfile -   app:app   --chdir /app

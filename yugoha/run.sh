#!/usr/bin/with-contenv bashio
set -e

mkdir -p /data

# Start the API first. Supervisor discovery immediately launches the
# Home Assistant config flow, and that flow performs /api/health.
# If discovery is sent before gunicorn is listening, automatic setup aborts.
/opt/yugoha-venv/bin/gunicorn \
  --bind 0.0.0.0:8099 \
  --workers 1 \
  --threads 8 \
  --timeout 60 \
  --access-logfile - \
  --error-logfile - \
  app:app \
  --chdir /app &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup TERM INT EXIT

# Wait until the local API is really ready before registering discovery.
for i in $(seq 1 30); do
  if /opt/yugoha-venv/bin/python - <<'PY'
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8099/api/health', timeout=1).read()
PY
  then
    echo "[yuGoHA] local API is ready"
    break
  fi
  sleep 1
done

# Install/update bundled integration and register Supervisor discovery.
# When integration files changed, bootstrap asks Core to restart once.
/opt/yugoha-venv/bin/python /app/bootstrap.py || true

# Keep message ids unique across server reinstalls/moves.
/opt/yugoha-venv/bin/python /app/id_migration.py || true

wait "$SERVER_PID"

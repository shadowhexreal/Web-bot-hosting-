#!/bin/sh
# Starts SHADOW HOSTING inside the container (Railway or plain Docker).
set -e

# Where users.json, bots/, runtime/ ... live. On Railway this is the attached Volume.
DATA_DIR="${DATA_DIR:-${RAILWAY_VOLUME_MOUNT_PATH:-/data}}"
mkdir -p "$DATA_DIR"
cd "$DATA_DIR"

if env | grep -q '^RAILWAY_'; then
  export COOKIE_SECURE=1               # Railway always serves https
  export CLIENT_IP_HEADER=X-Real-IP    # the client IP header set by Railway's edge
  if [ -z "$RAILWAY_VOLUME_MOUNT_PATH" ]; then
    echo "WARNING: no Railway Volume is attached. All data is lost on every deploy/restart."
    echo "         Add a Volume with mount path /data (Service -> Settings -> Volumes)."
  fi
fi

# Stable secret for login cookies (kept on the volume unless SECRET_KEY is set)
if [ -z "$SECRET_KEY" ]; then
  if [ ! -f .secret_key ]; then
    (umask 077; head -c 48 /dev/urandom | base64 | tr -d '\n=/+' > .secret_key)
  fi
  SECRET_KEY="$(cat .secret_key)"
  export SECRET_KEY
fi

echo "SHADOW HOSTING starting on port ${PORT:-8080} (data: $DATA_DIR)"
# One worker on purpose: bot processes, live links and rate limits are kept in memory.
exec gunicorn app:app \
  --chdir "$DATA_DIR" --pythonpath /app \
  --bind "0.0.0.0:${PORT:-8080}" \
  --workers 1 --threads 16 --timeout 300 --graceful-timeout 20 \
  --access-logfile -

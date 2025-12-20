#!/bin/bash
set -e

# Set timezone for Python
export TZ="${TZ:-UTC}"
ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

echo "[Entrypoint] Waiting for PostgreSQL to be ready..."

# Use Python script to wait for database
python3 tools/wait_for_db.py || {
  echo "[Entrypoint] ERROR: PostgreSQL connection failed"
  exit 1
}

echo "[Entrypoint] PostgreSQL is ready!"

# Run database initialization
echo "[Entrypoint] Initializing database schema..."
python3 -c "from app.db import init_db; init_db()" || {
  echo "[Entrypoint] ERROR: Database initialization failed"
  exit 1
}

echo "[Entrypoint] Starting application..."

# If the provided command is 'gunicorn', compute dynamic worker/threads settings
if [ "$1" = "gunicorn" ]; then
    # Determine available CPUs inside the container (falls back to 1)
    CPUS=$(python - <<'PY'
import os
c = os.cpu_count() or 1
print(int(c))
PY
)

    # Allow explicit override
    if [ -n "${GUNICORN_WORKERS:-}" ]; then
        WORKERS=${GUNICORN_WORKERS}
    else
        MAX_WORKERS=${GUNICORN_MAX_WORKERS:-8}
        CALC_WORKERS=$(( 2 * CPUS + 1 ))
        if [ "$CALC_WORKERS" -gt "$MAX_WORKERS" ]; then
            WORKERS=$MAX_WORKERS
        else
            WORKERS=$CALC_WORKERS
        fi
    fi

    THREADS=${GUNICORN_THREADS:-2}
    TIMEOUT=${GUNICORN_TIMEOUT:-300}

    echo "[Entrypoint] Detected ${CPUS} CPUs -> starting Gunicorn workers=${WORKERS}, threads=${THREADS}, timeout=${TIMEOUT}"

    exec gunicorn --bind 0.0.0.0:8080 \
        --workers "${WORKERS}" --threads "${THREADS}" --timeout "${TIMEOUT}" \
        --access-logfile - --error-logfile - app.main:app
else
    exec "$@"
fi

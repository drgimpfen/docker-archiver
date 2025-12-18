#!/bin/bash
set -e

echo "[Entrypoint] Waiting for PostgreSQL to be ready..."

# Wait for PostgreSQL
max_attempts=30
attempt=0
until PGPASSWORD="${DATABASE_URL##*:}" psql -h "$(echo $DATABASE_URL | sed -E 's|.*@([^:/]+).*|\1|')" \
     -U "$(echo $DATABASE_URL | sed -E 's|.*://([^:]+):.*|\1|')" \
     -d "$(echo $DATABASE_URL | sed -E 's|.*/([^?]+).*|\1|')" -c '\q' 2>/dev/null; do
  attempt=$((attempt + 1))
  if [ $attempt -ge $max_attempts ]; then
    echo "[Entrypoint] ERROR: PostgreSQL not available after $max_attempts attempts"
    exit 1
  fi
  echo "[Entrypoint] Waiting for PostgreSQL... (attempt $attempt/$max_attempts)"
  sleep 2
done

echo "[Entrypoint] PostgreSQL is ready!"

# Run database initialization
echo "[Entrypoint] Initializing database schema..."
python3 -c "from app.db import init_db; init_db()" || {
  echo "[Entrypoint] ERROR: Database initialization failed"
  exit 1
}

echo "[Entrypoint] Starting application..."
exec "$@"

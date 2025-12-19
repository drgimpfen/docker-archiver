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
exec "$@"

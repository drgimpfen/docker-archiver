#!/bin/sh
set -e

# Wait for the database to be reachable (uses wait_for_db.py)
if [ -n "${DATABASE_URL:-}" ]; then
  echo "Waiting for database..."
  python /app/wait_for_db.py
fi

# Exec the command (default: gunicorn ...)
exec "$@"

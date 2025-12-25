# Development

This document explains how to set up a local development environment, run the app and tests, and work on the project.

---

## Prerequisites

- Git
- Python (3.10+ recommended; 3.11+ known to work)
- Docker & Docker Engine (with Buildx) and the `docker` CLI (Docker Compose v2; i.e. `docker compose`)
- (Optional) Redis if you want cross-worker SSE in multi-worker setups

---

## Quick start (recommended: Docker Compose)

1. Copy environment example and adjust secrets:

```bash
cp .env.example .env
# Edit .env: set DB_PASSWORD, SECRET_KEY, etc.
```

2. Start services (Postgres, Redis, app):

```bash
# start in background
docker compose up -d
# rebuild app if you changed code or Dockerfile
docker compose up -d --build --no-deps --remove-orphans app
```

3. Open the web UI at http://localhost:8080 and follow the initial setup to create an admin user.

Notes:
- The `docker-compose.override.yml.example` file contains development bind-mount examples (stacks, archives, redis-data).
- If you run multiple Gunicorn workers and want real-time SSE across workers, ensure the `redis` service is enabled and that `REDIS_URL` is present in the `app` environment.

---

## Local Python development (without Docker)

1. Create & activate a virtual environment:

```bash
python -m venv .venv
# POSIX
source .venv/bin/activate
# PowerShell (Windows)
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```bash
pip install -r requirements.txt
# install test runner if not installed
pip install pytest
```

3. Set the environment (POSIX / PowerShell examples):

POSIX (bash/zsh):

```bash
export DATABASE_URL="postgresql://user:pass@localhost:5432/docker_archiver"
export SECRET_KEY="dev-secret"
```

PowerShell:

```powershell
$env:DATABASE_URL = 'postgresql://user:pass@localhost:5432/docker_archiver'
$env:SECRET_KEY = 'dev-secret'
```

4. Initialize the database schema:

```bash
python -c "from app.db import init_db; init_db()"
```

5. Run the development server:

```bash
python app/main.py
```

The app will be available at http://0.0.0.0:8080

---

## Running tests

Install pytest if you haven't already (`pip install pytest`) and run:

```bash
pytest -q
```

Tips:
- Tests live under the `tests/` directory. You can run a single test file with `pytest tests/test_foo.py -q`.
- The tests assume you have the project dependencies installed. Consider running tests inside the virtualenv you created.

---

## Working with Docker during development

- Rebuild the `app` container after changing dependencies or the Dockerfile:

```bash
docker compose up -d --build --no-deps app
```

- If you need to run a quick one-off command inside the app container (for debugging or testing):

```bash
docker compose exec -T app /bin/sh -c "python -c 'print(\"hello\")'"
```

---

## Useful commands & tips

- Tail recent app logs:

```bash
docker compose logs -f --tail=200 app
```

- Show container mounts for diagnostics:

```bash
docker inspect <container_name> --format '{{json .Mounts}}'
```

- If you see bind-mount mismatch warnings in the Dashboard, make sure host and container paths are identical (see README / TROUBLESHOOTING.md).

---

## Project Structure

```
Docker-Archiver/
├── .github/                 # CI workflows and PR descriptions
│   └── workflows/           # GitHub Actions (publish-release.yml, publish-edge.yml)
├── app/                    # Application package
│   ├── notifications/       # Notification modules (adapters, handlers, sender, helpers)
│   ├── routes/              # Flask Blueprints (api/, history, settings, profile)
│   │   └── api/             # API endpoints (JSON/SSE/file responses)
│   ├── templates/           # Jinja2 templates (index.html, history.html, settings.html, ...)
│   ├── static/              # Static assets (icons, images, css)
│   ├── run_job.py           # Background job helper
│   ├── security.py          # Security/authorization helpers
│   ├── main.py              # Flask app & core routes
│   ├── db.py                # Database schema & connection
│   ├── executor.py          # Archive execution engine
│   ├── retention.py         # GFS retention logic
│   ├── stacks.py            # Stack discovery
│   ├── scheduler.py         # APScheduler integration
│   └── utils.py             # Utility functions
├── assets/                  # Repo assets (icons, images for packaging)
├── tools/                   # Utility scripts (wait_for_db.py, generate_favicon.py)
├── tests/                   # Pytest unit/integration tests
├── .env.example             # Environment template
├── docker-compose.yml       # Docker setup
├── docker-compose.override.yml.example
├── Dockerfile               # App container
├── entrypoint.sh            # Startup script
├── requirements.txt         # Python dependencies
├── API.md                   # External API reference (moved from README)
├── TROUBLESHOOTING.md      # Troubleshooting doc
├── SECURITY.md             # Security guidance & reporting
├── CHANGELOG.md            # Change history
└── README.md               # Project overview and quick start
```

---

## Contributing

See `CONTRIBUTING.md` for contribution guidelines, testing instructions, and the PR checklist. Keep PRs small and focused; if you change public APIs or the database schema, update `API.md` or `CHANGELOG.md` accordingly.

---

## Keeping this doc up-to-date

If you find instructions out of date, please update `DEVELOPMENT.md` and `README.md` or open a PR describing the change.

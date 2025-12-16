<img width="400" alt="image" src="https://github.com/user-attachments/assets/b21d95c0-3e01-4e9b-8652-9f14cf934ecc" />

# Docker-Archiver

**Project Overview**
- **Description:** Docker-Archiver is a small Flask web application that helps you archive Docker Compose stacks. It stops a stack, creates a TAR archive of the stack directory, restarts the stack, and stores archive metadata and job logs in a PostgreSQL database.

**Key Features**
- **Web UI:** Dashboard for discovering local stacks, starting archives, and viewing recent or full history.
- **Archive stacks:** Stops a Docker Compose stack, creates a .tar archive of the stack directory, then restarts the stack.
- **Background jobs & logging:** Archiving runs in background threads; each job is recorded in the `archive_jobs` table with logs appended during the process.
-- **Archive storage:** Archives are stored inside the container at `/archives` (mounted to a host path via Docker volumes in `docker-compose.yml`).
- **Retention cleanup:** Configurable retention (default 28 days) removes old `.tar` archives.
- **User management:** Initial setup for the first admin user, profile edit, and password change via the web UI.
- **Passkeys (WebAuthn):** Register and authenticate using passkeys (WebAuthn) in addition to password login.
- **Download & delete archives:** UI endpoints to download or delete specific archive files.
- **Postgres-backed settings:** Stores settings, users, passkeys and job metadata in Postgres.

**Important Files**
- **App entry & routes:** [app/main.py](app/main.py)
- **Archiving logic:** [app/backup.py](app/backup.py)
- **Dockerfile:** [Dockerfile](Dockerfile)
- **Docker Compose:** [docker-compose.yml](docker-compose.yml)
- **Python requirements:** [requirements.txt](requirements.txt)
- **Templates:** [app/static/templates](app/static/templates)

**Installation (Recommended: Docker)**
- Start with Docker Compose (recommended):

```bash
docker-compose up -d --build
```

- The default `docker-compose.yml` binds:
	- host Docker socket (`/var/run/docker.sock`) into the container
	- host stack directories under `/opt/stacks` or `/opt/dockge` into `/local` inside the container
	- backup directory `/var/backups/docker` (host) mounted to `/archives` (container) to persist archives

**Local development (without Docker)**
- Ensure `DATABASE_URL` is set to a running Postgres instance, then install dependencies and run:

```bash
pip install -r requirements.txt
python -m app.main
```

or run with Gunicorn (as the Dockerfile does):

```bash
# install dependencies
pip install -r requirements.txt
# run with gunicorn
gunicorn --bind 0.0.0.0:5000 main:app
```
"""
Docker Archiver
---------------

A small web app to stop/start Docker Compose stacks, create archives of stack directories, keep history, schedule backups, and send notifications via Apprise.

Features
--------
- Discover local Docker Compose stacks and archive them as .tar files
- Safe exclusion: the app avoids listing itself; you can force exclusion using a compose label (see below)
- Scheduler (APScheduler) with timezone support (set via `TZ` env)
**Project Overview**
**Description:** Docker-Archiver is a small Flask web application that helps you archive Docker Compose stacks. It stops a stack, creates a TAR archive of the stack directory, restarts the stack, and stores archive metadata and job logs in a PostgreSQL database.

**Key Features**
- **Web UI:** Dashboard for discovering local stacks, starting archives, and viewing recent or full history.
- **Archive stacks:** Stops a Docker Compose stack, creates a .tar archive of the stack directory, then restarts the stack.
- **Background jobs & logging:** Archiving runs in background threads; each job is recorded in the `archive_jobs` table with logs appended during the process.
- **Archive storage:** Archives are stored inside the container at `/archives` (mounted to a host path via Docker volumes in `docker-compose.yml`).
- **Retention cleanup:** Configurable retention (default 28 days) removes old `.tar` archives.
- **User management:** Initial setup for the first admin user, profile edit, and password change via the web UI.
- **Passkeys (WebAuthn):** Register and authenticate using passkeys (WebAuthn) in addition to password login.
- **Download & delete archives:** UI endpoints to download or delete specific archive files.
- **Postgres-backed settings:** Stores settings, users, passkeys and job metadata in Postgres.

**Important Files**
- **App entry & routes:** [app/main.py](app/main.py)
- **Archiving logic:** [app/backup.py](app/backup.py)
- **Dockerfile:** [Dockerfile](Dockerfile)
- **Docker Compose:** [docker-compose.yml](docker-compose.yml)
- **Python requirements:** [requirements.txt](requirements.txt)
- **Templates:** [app/static/templates](app/static/templates)

**Installation (Recommended: Docker)**
- Start with Docker Compose (recommended):

```bash
docker-compose up -d --build
```

- The default `docker-compose.yml` binds:
	- host Docker socket (`/var/run/docker.sock`) into the container
	- host stack directories under `/opt/stacks` or `/opt/dockge` into `/local` inside the container
	- backup directory `/var/backups/docker` (host) mounted to `/archives` (container) to persist archives

**Local development (without Docker)**
- Ensure `DATABASE_URL` is set to a running Postgres instance, then install dependencies and run:

```bash
pip install -r requirements.txt
python -m app.main
```

or run with Gunicorn (as the Dockerfile does):

```bash
# install dependencies
pip install -r requirements.txt
# run with gunicorn
gunicorn --bind 0.0.0.0:5000 main:app
```

Docker Archiver
---------------

A small web app to stop/start Docker Compose stacks, create archives of stack directories, keep history, schedule backups, and send notifications via Apprise.

Features
--------
- Discover local Docker Compose stacks and archive them as .tar files
- Safe exclusion: the app avoids listing itself; you can force exclusion using a compose label (see below)
- Scheduler (APScheduler) with timezone support (set via `TZ` env)
- Per-user theme preference (dark by default)
- Apprise notifications for success/failure (configurable in Settings)
- Postgres-backed job history and settings

Quick start (local build — no Docker Hub required)
-----------------------------------------------

1. Clone the repo and enter the folder:

```bash
git clone <repo> && cd Docker-Archiver
```

2. Create an environment file (`.env`) and set required variables (example values):

```ini
DATABASE_URL=postgresql://user:password@db:5432/archiver
TZ=UTC
# SECRET_KEY can be set, or the app will generate a random one for local runs
SECRET_KEY=replace_me
```

3. Build and run with Compose (builds image locally; nothing required on Docker Hub):

```bash
docker compose up -d --build
```
or if your system uses the legacy `docker-compose`:

```bash
docker-compose up -d --build
```

4. Open the UI at http://localhost:5000 and complete the initial admin setup.

Run without Compose (optional)
------------------------------

Build locally and run a container directly:

```bash
docker build -t docker-archiver:local .
docker run --rm -p 5000:5000 \
	-v /var/backups/docker:/archives \
	-v /var/run/docker.sock:/var/run/docker.sock \
	--env-file .env \
	docker-archiver:local
```

Mounts & security
-----------------

- The app needs access to the Docker socket to stop/start stacks: mount `/var/run/docker.sock` into the container.
- The backup archive directory inside the container is `/archives` (mount a host path to persist archives).
- Warning: mounting the Docker socket grants powerful privileges — run only in trusted environments.

Configuration highlights
----------------------

- `TZ`: Set the timezone (e.g. `Europe/Berlin`) for scheduler behavior.
- `DATABASE_URL`: PostgreSQL connection string.
- `LOCAL_STACKS_PATH`: path scanned for stacks (defaults to `/local` inside container).

Apprise notifications
---------------------

- Configure notifications in the web UI: Settings → Apprise. Enter one Apprise URL per line and toggle `apprise_enabled`.
- Documentation link is provided in Settings.

Excluding stacks from discovery
------------------------------

To ensure a stack is never listed/archived, add this label to the service (or top-level labels) in the stack's compose file:

Service-level example:

```yaml
services:
	myservice:
		labels:
			- "docker-archiver.exclude=true"
```

Top-level example:

```yaml
labels:
	docker-archiver.exclude: "true"
```

Supported keys: `docker-archiver.exclude`, `archiver.exclude`, `docker_archiver.exclude`. Values `true`, `1`, `yes`, `on` are recognized.

Scheduler & Schedules UI
------------------------

- Create schedules via the Schedules page: name, time (HH:MM), select stacks, retention days, enabled/disabled.
- The scheduler reads `TZ` for timezone-aware cron scheduling.

Notifications behavior
---------------------

- The app sends Apprise notifications on per-stack success/failure and a master notification after a run completes when enabled.

Development & troubleshooting
----------------------------

- Rebuild with dependencies if you change `requirements.txt`:

```bash
docker compose build --no-cache
docker compose up -d --build
```

- View logs:

```bash
docker compose logs -f app
```

DB migration note
-----------------

The app attempts to create and alter tables on startup (safe `IF NOT EXISTS`). You can run the following manually if needed:

```bash
docker compose exec db psql -U <db_user> -d <db_name> -c "ALTER TABLE users ADD COLUMN IF NOT EXISTS theme VARCHAR(20) DEFAULT 'dark';"
```

Contributing
------------

- Consider security when running with the Docker socket mounted.
- If you want per-user Apprise configs instead of the current global setting, I can add that.

---

If you want, I can also add an example `.env.example` file and a short troubleshooting section for common errors.
```

DB migration note
-----------------

The app attempts to create and alter tables on startup (safe `IF NOT EXISTS`). You can run the following manually if needed:

```bash
docker compose exec db psql -U <db_user> -d <db_name> -c "ALTER TABLE users ADD COLUMN IF NOT EXISTS theme VARCHAR(20) DEFAULT 'dark';"
```

Contributing
------------

- Consider security when running with the Docker socket mounted.
- If you want per-user Apprise configs instead of the current global setting, I can add that.

---

If you want, I can also add an example `.env.example` file and a short troubleshooting section for common errors.
"""

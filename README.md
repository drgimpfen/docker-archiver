<div align="center">
  <img src="app/static/images/Logo.png" alt="Docker Archiver Logo" width="400">
  
  # Docker Archiver
  
  A modern, web-based solution for automated Docker stack backups with GFS (Grandfather-Father-Son) retention, scheduling, and notifications.
  
  [![GitHub](https://img.shields.io/badge/GitHub-drgimpfen/Docker--Archiver-blue?logo=github)](https://github.com/drgimpfen/Docker-Archiver/)
  [![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2?logo=discord&logoColor=white)](https://discord.gg/Tq84tczrR2)
  
</div>

## Features

- 🗂️ **Archive Management** - Create and manage multiple archive configurations
- 📦 **Stack Discovery** - Automatically discovers Docker Compose stacks from mounted directories
- ⏱️ **Flexible Scheduling** - Cron-based scheduling with maintenance mode support
- 🔄 **GFS Retention** - Grandfather-Father-Son retention policy (keep X days/weeks/months/years)
- 🧹 **Automatic Cleanup** - Scheduled cleanup of orphaned archives, old logs, and temp files
- 🎯 **Dry Run Mode** - Test archive operations without making changes
- 📊 **Job History & Live Logs** - Detailed logs and metrics for all archive/retention runs. The **Job Details** modal includes live log tailing (polls `/api/jobs/<id>/log/tail`) and supports per‑job EventSource streaming for near‑real‑time log updates. **Note:** global Jobs SSE support for the dashboard has been removed; the dashboard uses polling for job status updates. The modal offers terminal-like controls (search, **Pause/Resume**, **Copy**, **Download**, **Line numbers**) for easier log inspection.
- 🔔 **Smart Notifications** - Email via SMTP (configured in **Settings → Notifications**; settings are stored in the database)
- 🌓 **Dark/Light Mode** - Modern Bootstrap UI with theme toggle
- 🔐 **User Authentication** - Secure login system (role-based access coming soon)
- 💾 **Multiple Formats** - Support for tar, tar.gz, tar.zst, or folder output
- 🛡️ **Output Permissions (configurable)** — Optionally have the application set sensible, secure defaults on files and directories it creates. When enabled, files will be writable only by the server process (other users can still read them) and directories will be readable and searchable so their contents are accessible. Toggle this in **Settings → Apply permissive permissions to generated archives** (default: disabled).
- 🌍 **Timezone Support** - Configurable timezone via environment variable

## Architecture

### Phased Execution

Each archive run follows a 4-phase process:

1. **Phase 0: Initialization** - Create necessary directories
2. **Phase 1: Stack Processing** - For each stack:
   - Check if running (via Docker API)
   - Stop containers (if configured and running)
   - Create archive (tar/tar.gz/tar.zst/folder)
   - Restart containers (if they were running)
3. **Phase 2: Retention** - Apply GFS retention rules and cleanup old archives
4. **Phase 3: Finalization** - Calculate totals, send notifications, log disk usage

See **How Stack Discovery Works** below for full details on how stack directories are detected and scanned.

## Quick Start

### 1. Clone and Configure

```bash
git clone https://github.com/drgimpfen/Docker-Archiver.git
cd Docker-Archiver
cp .env.example .env

# Optional: copy the override example for local development (bind-mounts / overrides)
cp docker-compose.override.yml.example docker-compose.override.yml
```

Edit `.env` and set:
- `DB_PASSWORD` - PostgreSQL password (required)
- `SECRET_KEY` - Flask session secret (required)

### 2. Start Services

```bash
docker compose up -d
```

The application will be available at **http://localhost:8080**

> **Recommended update workflow (pull & restart app service)**
>
> On development/test VMs it's convenient to use a compact, robust one-liner that pulls the repo, updates images, rebuilds the `app` service and tails recent logs:
>
> ```bash
> git pull --ff-only && sudo docker compose pull && sudo docker compose up -d --build --no-deps --remove-orphans app && sudo docker compose logs -f --tail=200 app
> ```
>
> **Note:** `--ff-only` prevents accidental merge commits; `--no-deps` + service target (`app`) limits disruption to other services.

> **Note:** Stack directories must be configured as **bind mounts** — typically in `docker-compose.yml` for production, or in `docker-compose.override.yml` for local development (see examples below).

### 3. Initial Setup

On first visit, you'll be prompted to create an admin account.

### 4. Configure Archives

1. Go to the **Dashboard** and use the Archive management card (Create / Edit / Delete) to configure archives.
2. Select stacks to backup
3. Configure schedule (cron expression)
4. Set retention policy (GFS: days/weeks/months/years)
5. Choose output format
6. Save and run manually or wait for schedule

## Stack Directory Configuration

**Easy Setup (MANDATORY):** Add your stack directory **bind mounts** to `docker-compose.yml` with **identical host:container paths** (e.g., `- /opt/stacks:/opt/stacks`). This is mandatory — if stacks are not mounted as identical bind mounts the archiver cannot discover them and jobs will fail.

### Automatic Detection

Docker Archiver auto-detects stack directories from bind mounts that are mounted into the archiver container. Detection is performed using (in order): `docker inspect` on the running container, and `/proc/self/mountinfo` as a robust fallback.

Key behavior:
- Only **bind mounts** are considered. Named Docker volumes are ignored.
- The scanner checks the **mount root** and **one level of subdirectories** (fixed behavior; this is not configurable).
- Hidden directories (starting with `.`) and special names like `archives` or `tmp` are excluded.
- Results are **deduplicated** by resolved path.
- Each discovered stack is annotated as **direct** (compose found at mount root) or **nested** (compose found in a subdirectory).
- See **⚠️ Important: Bind Mounts Required** below for mandatory bind-mount requirements.

### Volume Mounts (how to configure)

Add bind mounts for your stack directories in `docker-compose.yml`. Examples:

```yaml
services:
  app:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./archives:/archives
      - /opt/stacks:/opt/stacks
      - /srv/docker/stacks:/srv/docker/stacks
      - /home/user/docker:/home/user/docker
```

```yaml
services:
  app:
    volumes:
      # Docker socket (required for container management)
      - /var/run/docker.sock:/var/run/docker.sock
      
      # Archive output directory (adjust path as needed)
      - ./archives:/archives
      
      # Stack directories - ADD YOUR MOUNTS HERE:
      - /opt/stacks:/opt/stacks
      - /srv/docker/stacks:/srv/docker/stacks
      - /home/user/docker:/home/user/docker
```

### Local development (docker-compose.override.yml)

For local or development-specific mounts, put your bind mounts in `docker-compose.override.yml`. Docker Compose automatically merges this file with `docker-compose.yml` when you run `docker compose up`.

If you run Docker Archiver with multiple workers and want real-time SSE events to work across workers, run a Redis service and configure `REDIS_URL` (example using `docker-compose.override.yml` below).

Ensure host and container paths in your `docker-compose.override.yml` are identical — see **⚠️ Important: Bind Mounts Required** below for details.

```yaml
services:
  app:
    volumes:
      - /opt/stacks:/opt/stacks    # Host and container paths are the same
      - ./archives:/archives
```

Using identical paths (host:container) is **mandatory** to ensure the archiver finds compose files and runs `docker compose` from the expected working directory.

### Redis is included by default

Redis is now included by default in `docker-compose.yml` to enable cross-worker SSE event streaming out of the box. The service is a lightweight `redis:7-alpine` container and stores data in a bind-mounted folder at `./redis-data` so your Redis data is persisted next to the project.

If you need to disable Redis for a particular environment, remove the `redis` service from your compose file and unset `REDIS_URL` (or remove it from the `app` environment). Otherwise the app will automatically use the bundled Redis instance and set `REDIS_URL` to `redis://redis:6379/0` by default.

After changing compose files restart the app: `docker compose up -d --build app`. Verify `REDIS_URL` appears in the app logs on startup; the app automatically uses Redis when available.

**Recommendation:** It's sensible to include and run a lightweight Redis service by default (the override example adds `redis:7-alpine`). Running Redis even on single-node development setups makes the deployment future-proof (enables cross-worker SSE when you scale to multiple Gunicorn workers) and adds minimal resource overhead. If you prefer to omit Redis, remove the `redis` service from your compose file and either unset `REDIS_URL` or remove it from the `app` environment to disable cross-worker streaming.

**Optional Gunicorn environment overrides:** You can optionally control Gunicorn sizing via env vars in `docker-compose.yml` (they are commented examples in the provided compose). Example settings you can enable in `docker-compose.override.yml`:

```yaml
services:
  app:
    environment:
      # Override automatic sizing and force workers/threads
      # GUNICORN_WORKERS: "6"     # explicit workers (overrides auto calc)
      # GUNICORN_MAX_WORKERS: "8" # cap for auto sizing
      # GUNICORN_THREADS: "2"     # threads per worker
      # GUNICORN_TIMEOUT: "300"  # worker timeout in seconds
```


### How Stack Discovery Works

Discovery follows these rules:
- The app first **auto-detects** candidate mount points from bind mounts inside the archiver container.
- For each mount point the app checks the **mount root** and **one level of subdirectories** for compose files:
  - If a compose file is present at the mount root, the stack is marked as **direct**.
  - If a compose file is present in a subdirectory, the stack is marked as **nested** (the subdirectory becomes the stack path).
- The scanner **ignores** hidden directories (names that start with `.`) and obvious non-stack names like `archives` or `tmp` to reduce false positives.
- Results are deduplicated by resolved path so the same stack mounted multiple ways is only listed once.

**Behavior for non-mounted stacks:** If a stack directory is not available via a bind mount, the archiver will use the path as it appears inside the container (the container-side path) when running compose commands; it will not attempt to use host-only paths that are not mounted into the container.

See **⚠️ Important: Bind Mounts Required** below for the mandatory bind-mount rules — mismatched mounts will be ignored and may cause job failures.

<a name="troubleshooting-bind-mount-warnings"></a>
### Troubleshooting bind-mount warnings

> Note: The real-time EventSource (SSE) stream is in-memory by default and is only guaranteed to work when the job runs in the same process that serves the SSE request. For multi-worker deployments use a central pub/sub (e.g., Redis) and set `REDIS_URL` in your environment to enable cross-worker event streaming.

If you see a dashboard warning about bind-mount mismatches or a job aborting with a **"No valid stacks found"** message, check the following:

- Inspect container mounts on the host: `docker inspect <container>` or `docker inspect <container> --format '{{json .Mounts}}'`. Verify entries show `"Type": "bind"` and that the **host/source path and container/destination path are identical**.
- Ensure you defined the bind mounts in `docker-compose.yml` or `docker-compose.override.yml` and that you copied `docker-compose.override.yml.example` to `docker-compose.override.yml` for local development when needed.
- After changing compose files, restart the app service: `docker compose up -d --build app` and check the Dashboard for the warning to disappear.
- Check application logs and the job log: the job log will include an explicit message when no valid stacks are found explaining that bind mounts are mandatory.

If the issue persists, open an issue and include your mount output and relevant logs so we can help troubleshoot.

### ⚠️ Important: Bind Mounts Required

**Stack directories MUST use bind mounts** (not named volumes):

✅ **Correct:**
```yaml
services:
  app:
    volumes:
      - /opt/stacks:/opt/stacks  # Bind mount (host:container - same path)
```

❌ **Incorrect:**
```yaml
- my-volume:/opt/stacks    # Named volume - will NOT work
```

**How it works:** Docker Archiver uses the configured `STACKS_DIR` paths directly. When it finds a stack at `/opt/stacks/immich`, it uses `/opt/stacks/immich` as the working directory for `docker compose` commands (since host and container paths are identical).

**Important (MANDATORY):** Bind mounts are **required** and **host and container paths must be identical** (for example: `- /opt/stacks:/opt/stacks`). Docker Archiver scans each mount root and one level of subdirectories for compose files. Any mounts where the host and container paths differ will be ignored for discovery, and jobs relying on those mounts may fail; configure proper bind mounts to ensure reliable discovery and correct `docker compose` execution.

**Note:** Named volumes *within* your stack's compose.yml (like `postgres_data:`) work perfectly fine - this requirement only applies to mounting the stack directories into the archiver container.

**Postgres data directory:** The project's `docker-compose.yml` uses a host bind mount for Postgres data at `./postgres-data:/var/lib/postgresql/data`. For reliable backups prefer `pg_dump` (recommended). If you need a file-level backup, stop the `db` container and copy `./postgres-data` (or use filesystem snapshots) to avoid partial writes.

---

<a name="bind-mounts"></a>
### Bind mounts — required configuration

For reliable discovery and correct `docker compose` execution, the host path and container path of your stack directory bind mounts **must be identical** (for example: `- /opt/stacks:/opt/stacks`).

Why this matters:

- Docker Archiver runs `docker compose` commands inside the container and expects to find the stack's compose files at the same path it discovered. If the host and container paths differ, the app tries to infer the host path from mounts, but this can lead to ambiguities or failures when running `docker compose` (e.g., when the host path is not accessible inside the container).
- Using identical paths avoids edge cases and ensures that archive and docker-compose commands run from the correct working directory.

**Bind-mount mismatch detection:** The archiver will now detect bind-mount mismatches (host path != container path). When mismatches are detected, the dashboard shows a prominent warning and those mounts will be ignored for discovery; if an archive job resolves to no valid stacks because of ignored mounts, the job will abort early and be marked as failed with a clear log message ("No valid stacks found"). To avoid this, **host:container bind mounts must be identical**.

Examples:

- Required: `- /opt/stacks:/opt/stacks` (host and container paths match)
- Not supported: `- /home/stacks:/opt/stacks` or `- /opt/stacks:/local/stacks` (host and container paths differ)

For more details and troubleshooting tips, see the dashboard warning messages or open an issue in the project repository.

## Configuration

### Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `TZ` | Europe/Berlin | No | Timezone for the application (e.g., America/New_York, Asia/Tokyo) |
| `DB_PASSWORD` | changeme123 | Yes | PostgreSQL password |
| `SECRET_KEY` | (dev key) | Yes | Flask session secret (change in production!) |
| `REDIS_URL` | - | No | Optional Redis URL (e.g., `redis://localhost:6379/0`) to enable cross-worker SSE event streaming |
| `DOWNLOADS_AUTO_GENERATE_ON_ACCESS` | false | No | When `true`, visiting a missing download link can trigger automatic archive generation on demand. Default: `false` (recommended). |
| `DOWNLOADS_AUTO_GENERATE_ON_STARTUP` | false | No | When `true`, the app attempts to generate missing downloads for valid tokens during startup (use with caution). Default: `false` (recommended). |
| `LOG_LEVEL` | INFO | No | Global log level for application logging (DEBUG, INFO, WARNING, ERROR). Set `LOG_LEVEL=DEBUG` to enable debug-level output for troubleshooting. |

> **Note:** Port (8080) and mount paths are configured in `docker-compose.yml`, not via environment variables.

> **Note:** The application uses fixed internal paths for archive output and logs. To ensure log files are persisted on the host, mount a directory to `/var/log/archiver` (e.g. `./logs:/var/log/archiver` in `docker-compose.yml`). The relevant internal paths are:
> - Archives directory: `/archives` (used for storing generated archives)
> - Logs directory: `/var/log/archiver/` (contains the central app log at `/var/log/archiver/app.log` and job logs under `/var/log/archiver/jobs/`, e.g. `/var/log/archiver/jobs/job_123_My_Archive_20251225_182530.log`)

### Logging & Debugging 🔧

Control application-wide logging using the `LOG_LEVEL` environment variable (recommended values: `DEBUG`, `INFO`, `WARNING`, `ERROR`). Setting `LOG_LEVEL=DEBUG` enables detailed diagnostic messages across components (scheduler, SSE, executor, etc.).

File logging is always enabled and organized as follows:
- Central app log: `/var/log/archiver/app.log` (rotated daily at midnight).
- Job logs:
  - Archive runs: `/var/log/archiver/jobs/archive_{archive_id}_{job_type}_{archive_name}_{TIMESTAMP}.log` (e.g. `archive_123_archive_My_Archive_20251225_182530.log`)
  - Cleanup runs: `/var/log/archiver/jobs/cleanup_{job_id}_{TIMESTAMP}.log` (e.g. `cleanup_42_20251225_020500.log`)
  - Notification attachments: job logs attached to notifications use `job_{job_id}_{archive_name}_{TIMESTAMP}.log`.

  Note: timestamps use local display timezone and format `YYYYMMDD_HHMMSS` (e.g., `20251225_182530`).

  Migration note: older versions wrote `cleanup_{job_id}.log` or `{archive_id}_{archive_name}.log` — these files are left in place; new runs will create timestamped filenames. Notification code prefers the most recent matching file for `cleanup_{job_id}_*.log` when present.

Rotation (daily) applies to the app and job logs; long-term retention/deletion of rotated files is handled by the application's cleanup job (configured via **Settings → Cleanup log retention days**).

Important: a logger set to a given level will **also include messages at higher-severity levels**. For example:

- `LOG_LEVEL=INFO` emits **INFO**, **WARNING**, **ERROR**, **CRITICAL**
- `LOG_LEVEL=DEBUG` emits **DEBUG**, **INFO**, **WARNING**, **ERROR**, **CRITICAL**

Quick examples:

```bash
# Temporarily enable debug for a single run
LOG_LEVEL=DEBUG docker compose up -d

# Persist in .env (recommended for long-running environments)
echo "LOG_LEVEL=DEBUG" >> .env
docker compose up -d
```

Tips:
- Use `DEBUG` for troubleshooting; use `INFO` for normal production verbosity.
- Expensive debug-only work is guarded by `logger.isEnabledFor(logging.DEBUG)` to avoid runtime overhead unless debug is explicitly enabled.

Deployment note: ensure the container can persist and write logs by mounting a host directory into `/var/log/archiver`. Example in `docker-compose.yml`:

```yaml
services:
  app:
    volumes:
      - ./logs:/var/log/archiver   # persist app and job logs on the host
```

This ensures job logs and cleanup summaries are available on the host for inspection and backups.

> **Note:** Redis is required for reliable cross‑worker SSE propagation. Set `REDIS_URL` (e.g., `redis://redis:6379/0`) and ensure the `redis` Python package is available. The app assumes Redis is present for real‑time streaming and global event propagation.

> **Note:** On startup the app will mark any jobs still in `running` state that **do not have an `end_time`** as `failed` to avoid stuck jobs and UI confusion; this behavior is automatic and not configurable via environment variables.


### Troubleshooting — Logs & Notifications 🔧

If you cannot find notification output (e.g., Discord, Email) in the normal container logs, check the job log files under `/var/log/archiver` — scheduled and detached jobs write their stdout/stderr to job log files there.

Why? Scheduled archive jobs are executed as detached subprocesses (see `app.run_job`) and their stdout/stderr are redirected into separate log files so the job can run independently of the web worker process. For this reason you will often find notification traces in those logs rather than in the central `docker compose logs` output.

Quick commands (run on host):

- List recent job logs:

```bash
docker compose exec -T app ls -ltr /var/log/archiver | tail -n 10
```

- Tail a specific job log and filter for notification entries:

```bash
docker compose exec -T app tail -n 300 /var/log/archiver/<JOB_LOG_FILE>.log | grep -E "Notifications:|Sent Email"
```

- Follow central app logs (shows what the web worker emits):

```bash
docker compose logs -f --tail=200 app
# or
docker compose exec -T app journalctl -u docker -n 200  # if you use systemd/journald integration
```

- Run a manual in-container test (writes to container logs and/or job log files depending on context):

```bash
docker compose exec -T app python -c "from app.main import app; ctx=app.app_context(); ctx.push(); from app.notifications.handlers import send_archive_notification; send_archive_notification({'name':'Test-Archive'}, 9999, [], 1, 0); ctx.pop()"
```

What to look for in logs:

- `Notifications: send_archive_notification called ...` — entry point from the archiver into the notifications code
- `Notifications: added target ...` — notification targets that were configured and accepted
- `Notifications: Sent Email ...` — email delivery success messages
- `Notifications: notification failed` / exception traces — indicates a problem sending an email (check SMTP credentials, network access, recipient validity)

Tips:

- Set `LOG_LEVEL=DEBUG` in `.env` if you need more detailed debugging output. Restart the `app` service after changing env vars.
- `docker compose run --rm` spawns a short-lived container and its logs may not appear in `docker compose logs`; prefer `docker compose exec` or inspect the logs under `/var/log/archiver` for scheduled runs.
- If you want important job summaries to also appear in the container's central logs, consider running a short command at the end of a job to emit a compact summary to stderr (this is a safe, small change we can help implement).

---

### Image pull policy

**Image pull policy:** The app lets you choose whether images should be pulled when stacks are restarted. You can find the option on **Settings → Security** as a single checkbox **Always pull images on start** (default: **disabled**, i.e. *Never*). In short:

- **Never (default)** — Do not pull images automatically. If required images are missing locally, the stack restart will be skipped and the job notification explains which stacks were skipped and why. The executor will append `--pull=never` to `docker compose up` to explicitly prevent pulls when restarting stacks.
- **Always** — Try to pull images before starting each stack. The executor runs `docker compose pull` and will record the pull output in the job log.

Pull inactivity timeout: To avoid a hung image pull blocking a whole job, the executor now uses an *inactivity timeout* (seconds) which aborts a pull if no output is produced for the configured period. You can set **Pull inactivity timeout (seconds)** in **Settings → Security** (default: **300**). Set it to **0** to disable the inactivity timeout (use with caution).

Notification note: When images are pulled, notifications include the full filtered pull output inline (HTML‑escaped), so operators can see the final result directly in the message. The excerpt filters out transient progress/spinner lines and keeps final, meaningful lines (for example: “[+] Pulling …”, “✔ … Pulled”, “Already exists”, “Download complete”, digest/sha256 lines). The full raw pull output is also stored in the job log and included as an attachment when log attachments are enabled; partial output is preserved if a pull times out or fails.

Notes:

- Pulls can fail for network or authentication reasons; the job log will contain details to help debugging.
- For deterministic production behavior we recommend pre-pulling images on hosts or using the **Always** option only when appropriate for your environment.
- The archiver records skipped stacks and reasons in the job summary so operators can act on them.

---

### Retention Policy

**GFS (Grandfather-Father-Son)**:
- **Keep Days**: Daily archives for last X days
- **Keep Weeks**: One archive per week for last X weeks
- **Keep Months**: One archive per month for last X months
- **Keep Years**: One archive per year for last X years

**One Per Day Mode**: When enabled, keeps only the newest archive per day (useful for test runs).

### Cron Expressions

Format: `minute hour day month day_of_week`

Examples:
- `0 3 * * *` - Daily at 3:00 AM
- `0 2 * * 0` - Weekly on Sunday at 2:00 AM
- `0 4 1 * *` - Monthly on 1st at 4:00 AM
- `*/30 * * * *` - Every 30 minutes

## Notifications

Docker Archiver sends notifications via **email (SMTP)** only. **SMTP settings are configured in the web UI under _Settings → Notifications_ and are stored in the application database (not via environment variables).** This avoids leaking credentials in environment files and makes runtime changes available via the web UI.

### SMTP Settings (in the UI)

- **SMTP Server** — Hostname or IP of your SMTP server (`smtp_server`)
- **SMTP Port** — TCP port for SMTP (`smtp_port`, commonly `587`)
- **SMTP Username** — Username for SMTP authentication (optional)

### Download Tokens & Notifications

The download feature uses short-lived tokens (24 hours) that map to either an existing archive file or an archive directory (for which the server may prepare a `.tar.zst`). Important behavior:

- **Token lifecycle**: Tokens are created via the API or UI and expire after 24 hours. A token may reference an existing file (`file_path`) or an archive directory (`archive_path`) that needs packing.
- **notify_emails**: Each token stores a `notify_emails` array (one or more recipients). When an archive becomes ready, the server sends a link notification to the addresses in `notify_emails`.
- **Public regenerate**: If an archive file is missing, the public `/download/<token>` page shows a small form where a user can submit a **single** email address to be added to `notify_emails` and request regeneration. The UI intentionally does **not** prefill any addresses.
- **Automatic generation flags**: Two environment flags control optional automatic generation:
  - `DOWNLOADS_AUTO_GENERATE_ON_ACCESS=false` (default) — when `true`, accessing a missing download link will attempt to start generation immediately.
  - `DOWNLOADS_AUTO_GENERATE_ON_STARTUP=false` (default) — when `true`, the app will look for tokens missing prepared archives on startup and start generation for them. Both defaults are `false` and we recommend leaving them **disabled** unless you understand and want automatic generation behavior.

Security / operational notes:
- The app uses **atomic DB updates** to avoid duplicate concurrent packing jobs for the same token and **database-backed notify_emails** for reliable notification targets.
- For multi-instance/HA deployments, consider using a task queue/worker instead of the built-in thread-based packer; this makes pack jobs deterministic and scalable.

- **SMTP Password** — Password for SMTP authentication (optional)
- **From address** — The sender `From:` address used for outgoing messages (`smtp_from`)
- **Use TLS** — Toggle to use STARTTLS for the SMTP connection (`smtp_use_tls`)

Recipients are taken from user profile email addresses or from configured default recipient settings in the Notifications page.

### Notification Options

- **Report verbosity** — Choose **Full** (default) or **Short** reports. Full includes a detailed HTML report with tables; Short sends a concise summary suitable for chat-like channels.
- **Attach full job log** — When enabled, the full job log will be attached as a downloadable `.log` file instead of being inlined in the message.
- **Attach log on failures only** — When enabled, the log is attached only when the job had failures.
- **Subject tag** — Optional prefix (e.g., `[Production]`, `[TEST]`) to add to the message subject.
- **Send Test Notification** — Use this button on the Notifications page to validate your SMTP settings and recipient delivery.

**Note:** Non-email transports (Apprise, Discord, etc.) are no longer supported — the app sends notifications via SMTP only. If you previously used Apprise URLs, migrate those recipient addresses into user profiles or the Notifications page accordingly.

## API Documentation

### External API (for automation/integrations)

All external API endpoints are located under `/api/*` and support **Bearer token authentication**.

#### Authentication

Generate an API token in your user profile (coming soon) or use session-based authentication from the web UI.

**Header Format:**
```
Authorization: Bearer <your-api-token>
```

#### Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| **Archives** |
| `/api/archives` | GET | Token/Session | List all archive configurations |
| `/api/archives/<id>/run` | POST | Token/Session | Trigger archive execution |
| `/api/archives/<id>/dry-run` | POST | Token/Session | Run simulation (dry run) |
| **Jobs** |
| `/api/jobs` | GET | Token/Session | List jobs (supports filters: `?archive_id=1&type=archive&limit=20`) |
| `/api/jobs/<id>` | GET | Token/Session | Get job details with stack metrics |
| `/api/jobs/<id>/download` | POST | Token/Session | Request archive download (generates token) |
| `/api/jobs/<id>/log` | GET | Token/Session | Download job log file |
| `/api/jobs/<id>/log/tail` | GET | Token/Session | Return incremental log lines for a job (query params: `last_line`, optional `stack`), supports live in-memory buffer and DB fallback for multi-worker setups |
| **Stacks** |
| `/api/stacks` | GET | Token/Session | List discovered Docker Compose stacks |
| **Downloads** |
| `/download/<token>` | GET | **None** | Download archive file (24h expiry) |

#### Example Usage

```bash
# List all archives
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://your-server:8080/api/archives

# Trigger archive execution
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" \
  http://your-server:8080/api/archives/1/run

# Get job details
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://your-server:8080/api/jobs/123

# List recent jobs
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "http://your-server:8080/api/jobs?type=archive&limit=10"

# Request download
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"stack_name":"mystack","archive_path":"/archives/path"}' \
  http://your-server:8080/api/jobs/123/download

# Download archive (no auth needed)
curl -O http://your-server:8080/download/abc123token
```

### Web UI Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard |
| `/login` | GET/POST | Login page |
| `/logout` | GET | Logout |
| `/setup` | GET/POST | Initial user setup |
| `/api/archives/create` | POST | Create archive config (via Dashboard/API) |
| `/api/archives/<id>/edit` | POST | Edit archive config (via Dashboard/API) |
| `/api/archives/<id>/delete` | POST | Delete archive config (via Dashboard/API) |
| `/api/archives/<id>/run` | POST | Run archive job (API) |
| `/api/archives/<id>/dry-run` | POST | Run dry run (API) |
| `/history/` | GET | Job history UI |
| `/profile/` | GET/POST | User profile (password, email) |
| `/settings/` | GET/POST | Settings page |
| `/health` | GET | Health check |

### Reverse Proxy Configuration (Pangolin, Authelia, etc.)

When using an authentication proxy like Pangolin or Authelia, you need to **exclude** the following paths from authentication:

```yaml
# Paths that should bypass authentication
exclude_paths:
  - /download/*         # Archive downloads (token-based, 24h expiry)
  - /api/*              # External API endpoints (use Bearer token auth)
  - /health             # Health check endpoint
```

**Note:** The `/api/*` endpoints have their own authentication via Bearer tokens. The download endpoint (`/download/<token>`) uses time-limited tokens and doesn't require session authentication.

Downloads are always prepared/stored under `/tmp/downloads` on the host container (this path is fixed). If a requested token points to an archive outside this directory, the application will attempt to regenerate a download file into `/tmp/downloads` before serving it. The application treats `/tmp/downloads` as a fixed, intentionally-ignored destination for bind-mount mismatch checks (similar to `/archives` and `/var/run/docker.sock`), so it will not appear in bind-mount mismatch warnings in the Dashboard. To persist generated downloads across container restarts, mount a host directory (for example `./downloads:/tmp/downloads`).

### Reverse proxy examples

For readable, centralized reverse proxy guidance and ready-to-copy examples for Traefik, Nginx / Nginx Proxy Manager, and Caddy, see `REVERSE_PROXY.md`.

> See: [REVERSE_PROXY.md](./REVERSE_PROXY.md) — includes SSE/WebSocket tips and recommended auth exclusions.



## Development

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment
export DATABASE_URL="postgresql://user:pass@localhost:5432/docker_archiver"
export SECRET_KEY="dev-secret"

# Initialize database
python -c "from app.db import init_db; init_db()"

# Run development server
python app/main.py
```

### Project Structure

```
Docker-Archiver/
├── app/
│   ├── routes/              # Flask Blueprints
│   │   ├── api/               # API endpoints (JSON/SSE/file responses)
│   │   │   ├── __init__.py    # Shared `bp` and auth helpers
│   │   │   ├── archives.py    # Archive CRUD routes (API)
│   │   │   ├── jobs.py        # Job listing, logs, tail
│   │   │   ├── downloads.py   # Download token generation & folder prep
│   │   │   ├── cleanup.py     # Cleanup runner endpoint
│   │   │   └── sse.py         # SSE endpoints
│   │   ├── history.py       # Job history routes
│   │   ├── settings.py      # Settings routes
│   │   └── profile.py       # User profile routes
│   ├── main.py              # Flask app & core routes
│   ├── db.py                # Database schema & connection
│   ├── auth.py              # User authentication
│   ├── executor.py          # Archive execution engine
│   ├── retention.py         # GFS retention logic
│   ├── stacks.py            # Stack discovery
│   ├── scheduler.py         # APScheduler integration
│   ├── downloads.py         # Download token system
│   ├── notifications.py     # Notifications (SMTP-only)
│   ├── utils.py             # Utility functions
│   ├── templates/           # Jinja2 templates
│   │   ├── base.html        # Base layout with navigation
│   │   ├── index.html       # Dashboard
│   │   ├── history.html     # Job history
│   │   ├── settings.html    # Settings page
│   │   ├── profile.html     # User profile
│   │   ├── login.html       # Login page
│   │   └── setup.html       # Initial setup
│   └── static/              # Static assets
│       ├── icons/           # GitHub, Discord, Favicon
│       └── images/          # Logo
├── docker-compose.yml       # Docker setup
├── Dockerfile               # App container
├── requirements.txt         # Python dependencies
├── entrypoint.sh            # Startup script
├── wait_for_db.py           # Database wait script
└── .env.example             # Environment template
```

## Database Schema

- **users** - User accounts
- **archives** - Archive configurations
- **jobs** - Archive/retention job records
- **job_stack_metrics** - Per-stack metrics within jobs
- **download_tokens** - Temporary download tokens (24h expiry)
- **settings** - Application settings (key-value)


## License

MIT License - see [LICENSE](LICENSE) file for details

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Support

- 🐛 **Issues**: https://github.com/drgimpfen/Docker-Archiver/issues
- 📚 **Documentation**: https://github.com/drgimpfen/Docker-Archiver/wiki
- 💬 **Discussions**: https://github.com/drgimpfen/Docker-Archiver/discussions
- 💬 **Discord**: https://discord.gg/Tq84tczrR2

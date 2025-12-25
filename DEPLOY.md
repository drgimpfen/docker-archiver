# Deployment Guide

## Quick Deploy

### 1. Prepare Server

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker & Docker Compose
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

### 2. Clone & Configure

```bash
git clone https://github.com/yourusername/docker-archiver.git
cd docker-archiver
cp .env.example .env
# Optional: copy the override example for local development
cp docker-compose.override.yml.example docker-compose.override.yml
nano .env
```

Set these variables:
```env
DB_PASSWORD=your-secure-db-password
SECRET_KEY=your-random-secret-key-here
APP_PORT=8080
ARCHIVE_DIR=/mnt/archives
# Optional download-generation flags (defaults: false)
# Leave these disabled unless you explicitly want auto generation behavior.
DOWNLOADS_AUTO_GENERATE_ON_ACCESS=false
DOWNLOADS_AUTO_GENERATE_ON_STARTUP=false
```

**Notes:**
- `DOWNLOADS_AUTO_GENERATE_ON_ACCESS`: when `true`, visiting a missing download link can start background generation immediately. Default is `false`.
- `DOWNLOADS_AUTO_GENERATE_ON_STARTUP`: when `true`, the app will attempt to generate missing downloads for valid tokens on startup. Default is `false` and should be used with caution.
- SMTP credentials are managed via the app UI (Settings → Notifications) and are not configured via env vars. See `README.md` for additional details.

### 3. Configure Volume Mounts

Add your stack directory **bind mounts** to `docker-compose.yml`. **This is mandatory** — host and container paths for the stack directories **must be identical** (e.g., `- /opt/stacks:/opt/stacks`). The application auto-detects bind mounts and scans them for compose files.

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - /mnt/backups:/archives
  - /opt/stacks:/opt/stacks        # ← Auto-detected (host:container paths must match)
  - /home/user/docker:/home/user/docker  # ← Auto-detected
```

If stacks are not mounted as identical bind mounts, the app may ignore them and jobs can fail. See the Dashboard bind-mount warnings and README troubleshooting section for guidance.

### 4. Start Services

#### Debugging (Enable debug logs)

To enable verbose logs for troubleshooting set `LOG_LEVEL=DEBUG` before running Docker Compose. This emits DEBUG-level messages (and higher) across the application components.

```bash
# Temporarily enable debug for a single run
LOG_LEVEL=DEBUG docker compose up -d

# Persist in .env for repeated troubleshooting
echo "LOG_LEVEL=DEBUG" >> .env
docker compose up -d
```

Use debug carefully in production — set it only when you need detailed diagnostics and revert to `INFO` afterwards.


Redis is included in `docker-compose.yml` by default and stores data in `./redis-data` (bind mounted). The `redis` service in the compose file is simple and lightweight, but we recommend adding a basic healthcheck to ensure the `app` waits for Redis to be ready before starting (example shown in compose). To start the services:

```bash
# Recommended update & start workflow (pull, update images, rebuild app service, tail logs)
git pull --ff-only && docker compose pull && docker compose up -d --build --no-deps --remove-orphans app && docker compose logs -f --tail=200 app
```

If you prefer a simple start for a fresh deployment:

```bash
docker compose up -d --build
```

**Optional: Add a Redis healthcheck** (example for `docker-compose.yml`):

```yaml
  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 3
    volumes:
      - ./redis-data:/data
```

### 5. Initial Setup

1. Browse to http://your-server:8080
2. Create admin account
3. Configure first archive
4. Configure SMTP for notifications

   * Go to **Settings → Notifications** in the web UI
   * Set **SMTP Server**, **SMTP Port**, **SMTP Username/Password** (if required), **From address** and toggle **Use TLS** as needed
   * Optionally add recipient addresses in user profiles or in the Notifications defaults
   * Use the **Send Test Notification** button to validate delivery

   Note: SMTP settings are stored in the application database (Settings) and are not configured via environment variables. This avoids leaking credentials in the environment and makes runtime changes available via the web UI.

## Production Hardening

### Gunicorn & Workers

The Docker image computes a sane default for Gunicorn workers on startup: the entrypoint detects CPU count and uses the formula **(2 * CPUS + 1)** to compute the worker count and caps it to `GUNICORN_MAX_WORKERS` (default **8**). This works well on machines with spare CPU capacity and avoids hardcoding worker counts in images.

You can override the automatic sizing with these environment variables:

- `GUNICORN_WORKERS` — set an explicit number of workers (overrides auto calculation)
- `GUNICORN_MAX_WORKERS` — maximum workers when using automatic sizing (default: 8)
- `GUNICORN_THREADS` — threads per worker (default: 2)
- `GUNICORN_TIMEOUT` — worker timeout in seconds (default: 300)

When running multiple workers and you rely on real-time SSE, ensure a cross-worker pub/sub is configured (e.g., Redis via `REDIS_URL`).

### Use HTTPS (Traefik Example)

```yaml
# docker-compose.yml
services:
  app:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.archiver.rule=Host(`archiver.example.com`)"
      - "traefik.http.routers.archiver.entrypoints=websecure"
      - "traefik.http.routers.archiver.tls.certresolver=letsencrypt"
    networks:
      - traefik
      - archiver-net

networks:
  traefik:
    external: true
```

> For more reverse proxy examples (Traefik, Nginx/NPM, Caddy) and tips for SSE/WebSocket, see `REVERSE_PROXY.md`.
# Backup

> Note: The compose file in this repository uses a host bind mount for Postgres at `./postgres-data:/var/lib/postgresql/data`. The preferred, consistent way to back up your database is using `pg_dump` (shown below). For a file-level backup, stop the `db` container before copying the `./postgres-data` directory to avoid partial writes.
>
> Example file-level backup (stop the DB first):
>
> ```bash
> docker compose stop db
> cp -r ./postgres-data ./postgres-data-backup
> docker compose start db
> ```

docker compose exec db pg_dump -U archiver docker_archiver > backup.sql

# Restore
docker compose exec -T db psql -U archiver docker_archiver < backup.sql
```

### Monitor Logs

```bash
# Follow logs
docker compose logs -f app

# Last 100 lines
docker compose logs --tail=100 app

# Filter errors
docker compose logs app | grep ERROR
```

## Updating

```bash
cd docker-archiver
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
```

## Troubleshooting

### Database Connection Issues

```bash
# Check DB container
docker compose ps db
docker compose logs db

# Test connection
docker compose exec db psql -U archiver -d docker_archiver -c '\l'
```

### Permission Issues

```bash
# Fix archive directory permissions
sudo chown -R 1000:1000 /mnt/archives

# Fix docker socket permissions
sudo chmod 666 /var/run/docker.sock
```

### Stack Not Discovered

1. Check mount paths in `docker-compose.yml`
2. Verify compose files exist:
   ```bash
   docker compose exec app ls -la /local/
   ```
3. Check logs:
   ```bash
   docker compose logs app | grep -i stack
   ```

### Redis Backup & Troubleshooting

- Backup Redis data (simple bind-copy):
  ```bash
  # Stop redis, copy dump, restart
  docker compose stop redis
  cp -r ./redis-data ./redis-data-backup
  docker compose start redis
  ```
- Or use `redis-cli` to create a snapshot:
  ```bash
  docker compose exec redis redis-cli SAVE
  # copy dump.rdb from ./redis-data
  ```

- Check Redis and REDIS_URL inside the app container:
  ```bash
  docker compose ps redis
  docker compose logs redis
  docker compose exec app env | grep REDIS_URL
  ```
- If SSE streaming doesn't propagate across workers, confirm `REDIS_URL` is set and reachable from the app.


### Archive Job Fails

1. Check job logs in History page
2. Verify docker socket access:
   ```bash
   docker compose exec app docker ps
   ```
3. Check disk space:
   ```bash
   df -h /archives
   ```

## Maintenance

### Cleanup Old Jobs

> **Note:** On startup the app will automatically mark jobs still in `running` state that **do not have an `end_time`** as `failed` to avoid stuck running states and UI confusion.

```sql
-- Connect to database
docker compose exec db psql -U archiver docker_archiver

-- Delete jobs older than 90 days
DELETE FROM jobs WHERE start_time < NOW() - INTERVAL '90 days';
```
### Cleanup Expired Tokens

Runs automatically daily at 2 AM, or manually:

```bash
docker compose exec app python -c "from app.downloads import cleanup_expired_tokens; cleanup_expired_tokens()"
```

### Reload Schedules

After changing maintenance mode or archive schedules:

```bash
docker compose restart app
```

# Troubleshooting

This document consolidates common troubleshooting steps and diagnostic commands for Docker Archiver.

## Bind-mount warnings & "No valid stacks found"

If you see a dashboard warning about bind-mount mismatches or a job aborting with a **"No valid stacks found"** message, check the following:

- Inspect container mounts on the host: `docker inspect <container>` or `docker inspect <container> --format '{{json .Mounts}}'`. Verify entries show `"Type": "bind"` and that the **host/source path and container/destination path are identical**.
- Ensure you defined the bind mounts in `docker-compose.yml` or `docker-compose.override.yml` and that you copied `docker-compose.override.yml.example` to `docker-compose.override.yml` for local development when needed.
- After changing compose files, restart the app service: `docker compose up -d --build app` and check the Dashboard for the warning to disappear.
- Check application logs and the job log: the job log will include an explicit message when no valid stacks are found explaining that bind mounts are mandatory.

If the issue persists, open an issue and include your mount output and relevant logs so we can help troubleshoot.

## Logs & Notifications

If you cannot find notification output (e.g., Email) in the normal container logs, check the job log files under `/var/log/archiver` — scheduled and detached jobs write their stdout/stderr to job log files there.

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
```

- Run a manual in-container test (writes to container logs and/or job log files depending on context):

```bash
docker compose exec -T app python -c "from app.main import app; ctx=app.app_context(); ctx.push(); from app.notifications.handlers import send_archive_notification; send_archive_notification({'name':'Test-Archive'}, 9999, [], 1, 0); ctx.pop()"
```

### Redis & SSE

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

## Additional troubleshooting tips

- Use `LOG_LEVEL=DEBUG` for detailed debugging output and restart the `app` service.
- Verify file permissions and disk space if writes fail.
- If a test release or workflow fails to push images, check Actions logs for auth issues and verify your registry tokens and permissions.

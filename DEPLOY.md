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
nano .env
```

Set these variables:
```env
DB_PASSWORD=your-secure-db-password
SECRET_KEY=your-random-secret-key-here
APP_PORT=8080
ARCHIVE_DIR=/mnt/archives
STACKS_DIR_1=/opt/stacks
```

### 3. Configure Volume Mounts

Edit `docker-compose.yml` to match your stack locations:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - /mnt/backups:/archives
  - /opt/stacks:/local/stacks
  - /srv/dockge:/local/dockge
```

### 4. Start Services

```bash
docker compose up -d
docker compose logs -f app
```

### 5. Initial Setup

1. Browse to http://your-server:8080
2. Create admin account
3. Configure first archive

## Production Hardening

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

### Backup Database

```bash
# Backup
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

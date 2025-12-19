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
- 📊 **Job History** - Detailed logs and metrics for all archive/retention runs
- 🔔 **Smart Notifications** - Apprise integration with customizable subject tags and HTML/text format
- 🌓 **Dark/Light Mode** - Modern Bootstrap UI with theme toggle
- 🔐 **User Authentication** - Secure login system (role-based access coming soon)
- 💾 **Multiple Formats** - Support for tar, tar.gz, tar.zst, or folder output
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

### Stack Discovery

The application scans `/local/*` directories (max 1 level deep) for Docker Compose files:
- `compose.yml` / `compose.yaml`
- `docker-compose.yml` / `docker-compose.yaml`

Stacks without compose files are skipped and logged.

## Quick Start

### 1. Clone and Configure

```bash
git clone https://github.com/drgimpfen/Docker-Archiver.git
cd Docker-Archiver
cp .env.example .env
```

Edit `.env` and set:
- `DB_PASSWORD` - PostgreSQL password (required)
- `SECRET_KEY` - Flask session secret (required)
- `SMTP_*` - Email/SMTP configuration (optional)

### 2. Start Services

```bash
docker compose up -d
```

The application will be available at **http://localhost:8080**

> **Note:** Stack directories must be configured in `docker-compose.yml` as volume mounts (see below).

### 3. Initial Setup

On first visit, you'll be prompted to create an admin account.

### 4. Configure Archives

1. Go to **Archives** → **Create Archive**
2. Select stacks to backup
3. Configure schedule (cron expression)
4. Set retention policy (GFS: days/weeks/months/years)
5. Choose output format
6. Save and run manually or wait for schedule

## Volume Mounts

**Important:** Stack directories are configured in `docker-compose.yml`, not via environment variables.

### Required Configuration

Edit `docker-compose.yml` and add your stack directories:

```yaml
services:
  app:
    volumes:
      # Docker socket (required for container management)
      - /var/run/docker.sock:/var/run/docker.sock
      
      # Archive output directory (adjust path as needed)
      - ./archives:/archives
      
      # Stack directories - ADD YOUR PATHS HERE:
      # Each mount point under /local/ will be scanned for stacks
      - /opt/stacks:/local/stacks           # Example: Subdirectories scanned
      - /opt/dockge/stacks:/local/dockge    # Example: Dockge stacks
      # - /srv/more-stacks:/local/more      # Add more as needed
```

### How Stack Discovery Works

The application scans `/local/*` directories (max 1 level deep):
- Direct compose file: `/local/mystack/compose.yml` → Stack: `mystack`
- Subdirectories: `/local/stacks/app1/compose.yml` → Stack: `app1`
- Multiple mounts: Each `/local/*` mount point is scanned independently

### ⚠️ Important: Bind Mounts Required

**Stack directories MUST use bind mounts** (not named volumes):

✅ **Correct:**
```yaml
services:
  app:
    volumes:
      - /opt/stacks:/local/stacks  # Bind mount (host:container)
```

❌ **Incorrect:**
```yaml
- my-volume:/local/stacks    # Named volume - will NOT work
```

**Why?** When Docker Archiver executes `docker compose` commands to stop/start stacks, these commands run on the Docker host (not inside the container). Relative paths in your stack's compose files (like `./library` or `./postgres`) must be resolved from the **host's perspective**.

**How it works:** Docker Archiver automatically detects the host path by reading `/proc/self/mountinfo`. When it sees `/local/stacks/immich` inside the container, it looks up the corresponding host path (e.g., `/opt/stacks/immich`) and uses that for `docker compose --project-directory`.

**Note:** Named volumes *within* your stack's compose.yml (like `postgres_data:`) work perfectly fine - this requirement only applies to mounting the stack directories into the archiver container.

## Configuration

### Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `TZ` | Europe/Berlin | No | Timezone for the application (e.g., America/New_York, Asia/Tokyo) |
| `DB_PASSWORD` | changeme123 | Yes | PostgreSQL password |
| `SECRET_KEY` | (dev key) | Yes | Flask session secret (change in production!) |
| `SMTP_SERVER` | - | No | SMTP server for email notifications (e.g., smtp.gmail.com) |
| `SMTP_PORT` | 587 | No | SMTP port |
| `SMTP_USER` | - | No | SMTP username |
| `SMTP_PASSWORD` | - | No | SMTP password/app-password |
| `SMTP_FROM` | - | No | Email sender address |

> **Note:** Port (8080) and mount paths are configured in `docker-compose.yml`, not via environment variables.

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

Docker Archiver uses [Apprise](https://github.com/caronc/apprise) for notifications.

### Supported Services

- Discord
- Telegram
- Email (SMTP)
- Slack
- Pushover
- Gotify
- And [100+ more](https://github.com/caronc/apprise#supported-notifications)

### Setup

**Option 1: Apprise URLs (Recommended)**
1. Go to **Settings** → **Notifications**
2. Add Apprise URLs (one per line):
   ```
   discord://webhook_id/webhook_token
   telegram://bot_token/chat_id
   ```
   **Note:** `mailto://` URLs are not allowed. Use SMTP environment variables for email notifications.
3. Select which events to notify:
   - Archive Success
   - Archive Error
   - Retention Cleanup
   - Cleanup Task
4. Optional: Add subject tag prefix (e.g., `[Production]`, `[TEST]`)
5. Optional: Toggle between HTML and Plain Text format
6. Test your configuration with the "Send Test Notification" button
7. Save settings

**Option 2: SMTP/Email (Automatic)**
1. Configure SMTP in `.env` file (see Environment Variables above)
2. Add email address in **Profile** page
3. All users with configured email addresses automatically receive notifications

**Important:** Do not use both SMTP environment variables AND Apprise `mailto://` URLs for the same email address, as this will result in duplicate notifications. Use SMTP environment variables for email, and Apprise for other services (Discord, Telegram, etc.).

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
| `/api/jobs` | GET | Token/Session | List jobs (supports filters: `?archive_id=1&type=archive&limit=50`) |
| `/api/jobs/<id>` | GET | Token/Session | Get job details with stack metrics |
| `/api/jobs/<id>/download` | POST | Token/Session | Request archive download (generates token) |
| `/api/jobs/<id>/log` | GET | Token/Session | Download job log file |
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
| `/archives/` | GET | Archive management UI |
| `/archives/create` | POST | Create archive config |
| `/archives/<id>/edit` | POST | Edit archive config |
| `/archives/<id>/delete` | POST | Delete archive config |
| `/archives/<id>/run` | POST | Run archive job |
| `/archives/<id>/dry-run` | POST | Run dry run |
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
  - /login              # Login page must be accessible
  - /setup              # Initial setup page
```

**Note:** The `/api/*` endpoints have their own authentication via Bearer tokens. The download endpoint (`/download/<token>`) uses time-limited tokens and doesn't require session authentication.

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
│   │   ├── archives.py      # Archive CRUD routes
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
│   ├── notifications.py     # Apprise/SMTP notifications
│   ├── utils.py             # Utility functions
│   ├── templates/           # Jinja2 templates
│   │   ├── base.html        # Base layout with navigation
│   │   ├── index.html       # Dashboard
│   │   ├── archives.html    # Archive management
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

## Roadmap

- [ ] Role-based access control (Admin/User/View-only)
- [ ] Email reports (scheduled summaries)
- [ ] Archive encryption
- [ ] Remote storage (S3, SFTP, etc.)
- [ ] Archive verification/testing
- [ ] Multi-language support
- [ ] REST API with token authentication
- [ ] Webhook triggers

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

# Docker Archiver

A modern, web-based solution for automated Docker stack backups with GFS (Grandfather-Father-Son) retention, scheduling, and notifications.

## Features

- 🗂️ **Archive Management** - Create and manage multiple archive configurations
- 📦 **Stack Discovery** - Automatically discovers Docker Compose stacks from mounted directories
- ⏱️ **Flexible Scheduling** - Cron-based scheduling with maintenance mode support
- 🔄 **GFS Retention** - Grandfather-Father-Son retention policy (keep X days/weeks/months/years)
- 🎯 **Dry Run Mode** - Test archive operations without making changes
- 📊 **Job History** - Detailed logs and metrics for all archive/retention runs
- 🔔 **Notifications** - Apprise integration for multi-service notifications
- 🌓 **Dark/Light Mode** - Modern Bootstrap UI with theme toggle
- 🔐 **User Authentication** - Secure login system (role-based access coming soon)
- 💾 **Multiple Formats** - Support for tar, tar.gz, tar.zst, or folder output

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
git clone https://github.com/yourusername/docker-archiver.git
cd docker-archiver
cp .env.example .env
```

Edit `.env` and set:
- `DB_PASSWORD` - PostgreSQL password
- `SECRET_KEY` - Flask session secret
- `ARCHIVE_DIR` - Where to store archives
- `STACKS_DIR_1` - Path to your Docker stacks

### 2. Start Services

```bash
docker compose up -d
```

The application will be available at http://localhost:8080

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

### Required Mounts

```yaml
volumes:
  # Docker socket (for container management)
  - /var/run/docker.sock:/var/run/docker.sock
  
  # Archive output directory
  - ./archives:/archives
  
  # Stack directories (adjust to your setup)
  - /opt/stacks:/local/stacks
```

### Multiple Stack Directories

You can mount multiple directories:

```yaml
volumes:
  # ... other mounts ...
  - /opt/stacks:/local/stacks        # Subdirectories scanned
  - /opt/dockge:/local/dockge       # Single stack
  - /srv/more-stacks:/local/more    # More subdirectories
```

The application will scan:
- `/local/stacks/*` (each subdir is a stack)
- `/local/dockge` (direct stack)
- `/local/more/*` (each subdir is a stack)

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | auto | PostgreSQL connection string |
| `SECRET_KEY` | required | Flask session secret |
| `APP_PORT` | 8080 | Application port |
| `ARCHIVE_DIR` | ./archives | Archive output directory |

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

1. Go to **Settings** → **Notifications**
2. Add Apprise URLs (one per line):
   ```
   discord://webhook_id/webhook_token
   telegram://bot_token/chat_id
   mailto://user:password@gmail.com
   ```
3. Select which events to notify
4. Save settings

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard |
| `/login` | GET/POST | Login page |
| `/setup` | GET/POST | Initial user setup |
| `/archives` | GET | Archive management |
| `/archives/create` | POST | Create archive config |
| `/archives/<id>/run` | POST | Run archive job |
| `/archives/<id>/dry-run` | POST | Run dry run |
| `/history` | GET | Job history |
| `/settings` | GET/POST | Settings page |
| `/api/job/<id>` | GET | Job details (JSON) |
| `/api/stacks` | GET | Discovered stacks (JSON) |
| `/download/<token>` | GET | Download archive (no auth) |
| `/health` | GET | Health check |

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
docker-archiver/
├── app/
│   ├── main.py           # Flask app & routes
│   ├── db.py             # Database schema & connection
│   ├── auth.py           # User authentication
│   ├── executor.py       # Archive execution engine
│   ├── retention.py      # GFS retention logic
│   ├── stacks.py         # Stack discovery
│   ├── scheduler.py      # APScheduler integration
│   ├── downloads.py      # Download token system
│   ├── notifications.py  # Apprise notifications
│   ├── templates/        # Jinja2 templates
│   └── static/           # CSS/JS assets
├── docker-compose.yml    # Docker setup
├── Dockerfile            # App container
├── requirements.txt      # Python dependencies
└── entrypoint.sh         # Startup script
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

- 🐛 **Issues**: https://github.com/yourusername/docker-archiver/issues
- 📚 **Documentation**: https://github.com/yourusername/docker-archiver/wiki
- 💬 **Discussions**: https://github.com/yourusername/docker-archiver/discussions

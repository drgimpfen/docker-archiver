# API Documentation (Reference)

This file contains the external API endpoints and example usage. For quick examples, refer to the README.

## External API (for automation/integrations)

All external API endpoints are located under `/api/*` and support **Bearer token authentication**.

### Authentication

Generate an API token in your user profile (coming soon) or use session-based authentication from the web UI.

**Header Format:**
```
Authorization: Bearer <your-api-token>
```

### Endpoints (selection)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/archives` | GET | Token/Session | List all archive configurations |
| `/api/archives/<id>/run` | POST | Token/Session | Trigger archive execution |
| `/api/jobs` | GET | Token/Session | List jobs (supports filters: `?archive_id=1&type=archive&limit=20`) |
| `/api/jobs/<id>` | GET | Token/Session | Get job details with stack metrics |
| `/api/jobs/<id>/log` | GET | Token/Session | Download job log file |
| `/api/jobs/<id>/log/tail` | GET | Token/Session | Return incremental log lines for a job (supports live buffers) |
| `/api/stacks` | GET | Token/Session | List discovered Docker Compose stacks |
| `/download/<token>` | GET | **None** | Download archive file (24h expiry) |

### Example Usage

```bash
# List all archives
curl -H "Authorization: Bearer YOUR_TOKEN" http://your-server:8080/api/archives

# Trigger archive execution
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" http://your-server:8080/api/archives/1/run

# Get job details
curl -H "Authorization: Bearer YOUR_TOKEN" http://your-server:8080/api/jobs/123
```

### Web UI Endpoints

List of key UI endpoints:
- `/` — Dashboard
- `/history/` — Job history UI
- `/settings/` — Settings page
- `/health` — Health check

For more detailed API reference (payloads, responses), refer to the full API docs in the project wiki or expand this file as needed.

# Reverse Proxy Examples

This document contains concise, practical reverse proxy examples for commonly used proxies (Traefik, Nginx / Nginx Proxy Manager, and Caddy) and notes to ensure streaming endpoints (SSE/WebSocket) and authentication exclusions work correctly.

## Paths to bypass authentication
When using a forward-auth proxy or authentication middleware, exclude the following paths from auth so the app functions correctly:

- `/download/*` (archive downloads, token-based)
- `/api/*` (external API endpoints — use Bearer token auth instead)
- `/health` (health checks)
- `/login` and `/setup` (initial setup and login pages)

## Traefik (v2) example

Add labels to the `app` service in `docker-compose.yml`:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.archiver.rule=Host(`archiver.example.com`)"
  - "traefik.http.routers.archiver.entrypoints=websecure"
  - "traefik.http.routers.archiver.tls.certresolver=letsencrypt"
  - "traefik.http.services.archiver.loadbalancer.server.port=8080"
```

Traefik supports streaming (SSE/WebSockets) by default; ensure any middleware or forward-auth configuration does not buffer or break streaming responses.

## Nginx / Nginx Proxy Manager example

For Nginx, ensure proxy buffering is disabled for streaming endpoints and headers are forwarded:

```nginx
location / {
    proxy_pass http://app:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
}
```

In Nginx Proxy Manager, add the `proxy_buffering off;` directive in the "Advanced" configuration for the host.

## Caddy example

Simple `Caddyfile` configuration:

```
archiver.example.com {
    reverse_proxy app:8080 {
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-For {remote}
    }
}
```

Caddy handles streaming well by default; ensure X-Forwarded headers are preserved.

## SSE / WebSocket Tips

- Disable proxy buffering for streaming endpoints (Nginx: `proxy_buffering off`).
- Use `proxy_http_version 1.1` and ensure `Connection` header is not forced to `close` to allow streaming.
- Preserve `Host` and `X-Forwarded-*` headers so the app can log and reconstruct client information.
- If you run the app with multiple workers and want cross-worker real-time streaming, configure a central pub/sub (Redis) and set `REDIS_URL` so SSE events are forwarded to all workers.

## Troubleshooting

- If streaming does not work: check proxy logs for buffering or header rewriting.
- Ensure `/api/*` is excluded from external auth and can be authenticated via Bearer tokens instead.


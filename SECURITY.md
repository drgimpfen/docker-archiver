# Security

This document summarizes security guidance for deploying and operating Docker Archiver and how to report vulnerabilities.

## Reporting vulnerabilities

If you discover a security issue, please **do not disclose it publicly**. Use one of the following:

- Preferably: open a **private** GitHub Security Advisory for this repository (recommended).
- Alternatively: open a private issue and mark it clearly as a security report.

Maintainers will triage security reports and respond as soon as possible. If you need a direct contact method, use the email address listed on the repository owner's GitHub profile.

---

## Quick production checklist

- Generate and store secrets securely (do not commit `.env` to source control).
  - Required secrets include: `SECRET_KEY` (Flask session secret), `DB_PASSWORD`, registry tokens (Docker Hub, GHCR), SMTP credentials.
  - Use Docker secrets, environment variables managed by your orchestrator, or GitHub Actions Secrets for CI. Rotate secrets regularly, and limit token scopes to the minimum needed.

- Run behind TLS (HTTPS) using a reverse proxy (Traefik, Nginx, Caddy, etc.).
  - Set TLS termination at the edge and enable HSTS. Ensure cookies are marked Secure and HttpOnly. Exclude paths required by the app from auth as documented in `REVERSE_PROXY.md`.

- Protect Docker socket usage
  - Mounting `/var/run/docker.sock` into containers grants powerful privileges. Only do so on trusted hosts and limit exposure. Consider running container management operations on a separate, dedicated host if possible.

- Bind mounts & file permissions
  - Only mount the directories needed (do not mount `/` or broad host paths).
  - Use identical `host:container` bind mounts for stacks (see README). Ensure archive and log directories have restrictive permissions.
  - Consider enabling the app's permission-application feature for archive outputs (`Settings → Apply permissive permissions to generated archives`) if you need automated fixes — review the generated permissions before enabling in production.

- Downloads & tokens
  - Download links are protected by short-lived tokens (24h by default). Do not publish tokens or logs that contain them.
  - `DOWNLOADS_AUTO_GENERATE_ON_STARTUP` should remain `false` unless you understand the operational implications.

- SMTP and notifications
  - Store SMTP credentials in the app settings (they are stored encrypted in the database, not in `.env`). Use a least-privilege SMTP account and enable TLS (STARTTLS) where possible. Test delivery with the UI test button.

- CI, registries & automation
  - Store `DOCKERHUB_TOKEN`, `DOCKERHUB_USERNAME`, `GITHUB_TOKEN` / PAT in repository or org secrets. If GHCR publishing is restricted in your organization, use a PAT with `write:packages` and rotate it periodically.
  - Avoid committing tokens or credentials in code or workflow files.
  - Consider adding image scanning (e.g., Trivy) and image signing (e.g., Cosign) to CI.

- Updates & monitoring
  - Keep base images, Python dependencies, and OS packages up to date. Use Dependabot or similar tooling to track vulnerable dependencies.
  - Enable logging and monitoring and periodically review logs for suspicious activity.

---

## Secure defaults recommended for Docker Archiver

- `SECRET_KEY` must be strong and unique per deployment (e.g., a 32+ byte random value).
- `DB_PASSWORD` must be strong and not shared across unrelated services.
- Do not allow anonymous access to `/api/*` or download links — the app uses tokens and Bearer authentication for those endpoints.

---

## Tools & references

- GitHub Security Advisories: https://docs.github.com/en/code-security/security-advisories
- Docker image scanning (Trivy): https://github.com/aquasecurity/trivy
- Container signing (Cosign): https://github.com/sigstore/cosign
- Best practices for secrets: https://12factor.net/config

---

If you'd like, I can add a short `SECURITY.md` entry to the repository settings (on GitHub) or add a maintainer contact email — tell me how you'd like to accept reports and I can add that info to this file.
# Changelog

## [0.8.2] - 2025-12-26
### Added
- Global SSE endpoint and client support for live updates to the dashboard, job details modal, and archive cards.
- Automatic DB timestamp migration routine to convert naive `TIMESTAMP` columns to `TIMESTAMPTZ` at startup (assumes stored values are UTC).

### Changed
- Modal update behavior: job details modal now refreshes in-place to prevent jumping/focus loss.
- Improved duration ticker and client parsing robustness.

### Fixed
- Consistent start/end time display: server sends ISO‑8601 UTC timestamps and client/server formatting is aligned.
- Fixed JS startup/await issues affecting SSE initialization.

## [0.8.1] - 2025-12-26
### Fixed
- Startup race where scheduler tried to load schedules before DB schema was visible (added schema readiness check and scheduler resilience).

## [0.8.0] - 2025-12-25
### Added
- Release workflows for Docker Hub and GHCR including short tag generation (vMAJOR, vMAJOR.MINOR) and edge scheduled builds.
- `TROUBLESHOOTING.md` and `API.md` reference docs (moved content from README).

### Changed
- Bumped app version to **0.8.0**.
- Removed legacy `/local` stack discovery fallback and consolidated bind-mount guidance.
- Notifications module refactored into `helpers`, `sender`, `handlers`.

### Fixed
- Dashboard job status live update bug; improved polling behavior.

### CI
- Added `publish-release.yml` and `publish-edge.yml` workflows; granted packages write permission for GHCR publishing.

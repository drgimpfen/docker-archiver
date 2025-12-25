# Contributing to Docker Archiver

Thank you for your interest in contributing! This project welcomes contributions of all kinds (bug reports, fixes, docs, tests, and features). Please follow the guidance below to make the process smooth.

## Quick checklist ✅
- Open an issue first for non-trivial changes or new features
- Fork the repository and create a branch (see naming below)
- Add or update tests for bugs and features
- Update `API.md`, `CHANGELOG.md` and docs if you change public behavior
- Ensure tests pass locally (`pytest -q`) and CI passes on your PR

## Branch & commit naming
- Branch naming: `feature/my-short-descr`, `fix/issue-123`, `docs/update-something`
- Use clear, descriptive commit messages. Prefer Conventional Commits style (e.g. `feat: add X`, `fix: correct Y`, `chore: bump deps`).

## Running tests locally
```bash
python -m venv .venv
# POSIX
source .venv/bin/activate
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pytest
pytest -q
```

## Writing tests
- Add tests under the `tests/` folder. Follow existing test styles (pytest + monkeypatch).
- Tests should run quickly and be deterministic. Use `tmp_path` fixtures for filesystem work.
- When fixing a bug, include a regression test that reproduces the failing behavior.

## Documentation
- Update `DEVELOPMENT.md`, `API.md`, `TROUBLESHOOTING.md`, or `README.md` when relevant.
- If you change public APIs or DB schema, also update `CHANGELOG.md` and mention migration/compatibility notes.

## CI & workflows
- All PRs should pass the repository's GitHub Actions workflows (build/publish tests are run on main when applicable).
- If your change affects CI or workflows, add clear instructions and tests where feasible and ask maintainers for review.

## Security disclosures
- Do **not** disclose security issues publicly. See `SECURITY.md` for instructions on reporting vulnerabilities privately.

## Review & merge
- Keep PRs small and focused. Large or breaking changes may be split into multiple PRs.
- Maintainence or larger changes require a brief description of the migration steps and any required downtime.

Thanks again — contributions make this project better for everyone! 🙌


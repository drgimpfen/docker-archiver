"""CLI entrypoint to run archive jobs as a detached subprocess.

Usage:
  python -m app.run_job --archive-id 42 [--dry-run] [--no-stop-containers] [--no-create-archive] [--no-run-retention]

This script loads the archive from the database and executes ArchiveExecutor.
It writes stdout/stderr to a per-job log under /var/log/archiver and relies on
executor.log() + DB persistence + SSE (Redis) for live tailing.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from app.db import get_db
from app.executor import ArchiveExecutor
from app import utils
from app.utils import setup_logging, get_logger

# Configure logging using centralized setup so LOG_LEVEL is respected
setup_logging()
logger = get_logger(__name__)


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive-id', type=int, required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--job-id', type=int, help='Existing job id to attach to this run (optional)')
    parser.add_argument('--no-stop-containers', action='store_true')
    parser.add_argument('--no-create-archive', action='store_true')
    parser.add_argument('--no-run-retention', action='store_true')
    # Optional: parent may pass an explicit log path so both parent and child know it
    parser.add_argument('--log-path', type=str, help='Optional path for the job log file')
    return parser.parse_args(argv or sys.argv[1:])


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    # Load archive
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM archives WHERE id = %s;", (args.archive_id,))
        archive = cur.fetchone()

    if not archive:
        logger.error("Archive id=%s not found", args.archive_id)
        sys.exit(2)

    # Build dry run config if requested
    dry_run_config = None
    if args.dry_run:
        dry_run_config = {
            'stop_containers': not args.no_stop_containers,
            'create_archive': not args.no_create_archive,
            'run_retention': not args.no_run_retention,
        }

    # Ensure job log dir exists
    jobs_dir = Path(os.environ.get('ARCHIVE_JOB_LOG_DIR', '/var/log/archiver'))
    try:
        jobs_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Determine log path: allow parent to pass a path via --log-path, otherwise
    # create a per-run timestamped filename using the same convention as before.
    timestamp = utils.local_now().strftime('%Y%m%d_%H%M%S')
    archive_name = archive['name']
    safe_name = utils.filename_safe(archive_name)
    job_type = 'dryrun' if args.dry_run else 'archive'
    default_log_name = f"{timestamp}_{job_type}_{safe_name}.log"
    default_log_path = jobs_dir / default_log_name

    # If the parent passed a log path (via CLI arg), use that. We'll parse args
    # early so we can accept --log-path.
    # Note: argparse parsing is at top; access attribute `log_path` if present.
    cli_log_path = None
    try:
        cli_log_path = args.log_path if hasattr(args, 'log_path') and args.log_path else None
    except Exception:
        cli_log_path = None

    if cli_log_path:
        log_path = Path(cli_log_path)
    else:
        log_path = default_log_path

    # Execute the job. If stdout has been redirected by the parent (e.g., API/scheduler
    # passed a log file), reuse inherited stdout/stderr so we don't create duplicate
    # or empty files. Otherwise, create a per-run log file and redirect stdout/stderr to it.
    import contextlib
    executor = ArchiveExecutor(dict(archive), is_dry_run=args.dry_run, dry_run_config=dry_run_config)

    try:
        # If stdout is not a TTY, it is likely redirected by the parent (file/pipe)
        if not sys.stdout.isatty():
            # Reuse inherited stdout/stderr
            try:
                executor.run(triggered_by='subprocess', job_id=args.job_id)
            finally:
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
        else:
            # No inherited redirection - create our own per-run log file
            with open(log_path, 'a', encoding='utf-8', errors='replace') as fh:
                with contextlib.redirect_stdout(fh), contextlib.redirect_stderr(fh):
                    executor.run(triggered_by='subprocess', job_id=args.job_id)
    except Exception as e:
        # Ensure exceptions are visible in the job log and app logs
        try:
            with open(log_path, 'a', encoding='utf-8', errors='replace') as fh:
                fh.write((str(e) + '\n'))
        except Exception:
            pass
        logger.exception("Job run for archive id=%s failed", args.archive_id)
        raise


if __name__ == '__main__':
    main()

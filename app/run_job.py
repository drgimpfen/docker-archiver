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
import logging

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

    # Ensure job log dir exists under LOG_DIR/jobs
    jobs_dir = Path(utils.get_log_dir()) / 'jobs'
    try:
        jobs_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Determine log path: allow parent to pass a path via --log-path, otherwise
    # create a per-run timestamped filename using UTC timestamp (keeps logs rotating by name)
    ts = utils.filename_timestamp()
    archive_name = archive['name']
    safe_name = utils.filename_safe(archive_name)
    job_type = 'dryrun' if args.dry_run else 'archive'
    # Use archive_{archive_id}_{job_type}_{archive_name}_{timestamp}.log for clarity
    default_log_name = f"archive_{args.archive_id}_{job_type}_{safe_name}_{ts}.log"
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

    # Build desired log_path: prefer provided --log-path, else per-archive filename
    log_path = None
    try:
        if cli_log_path:
            log_path = Path(cli_log_path)
        else:
            jobs_dir = Path(utils.get_log_dir()) / 'jobs'
            try:
                jobs_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            # Use archive_{archive_id}_{job_type}_{archive_name}_{timestamp}.log by default
            archive_id = args.archive_id
            log_path = jobs_dir / f"archive_{archive_id}_{job_type}_{safe_name}_{ts}.log"
    except Exception:
        log_path = Path(default_log_path)

    # Set up logging using centralized helper from utils
    try:
        job_logger, handler = utils.get_job_logger(args.archive_id, archive_name, log_path=str(log_path))
        # Redirect stdout/stderr to this logger if handler is available
        if handler is not None:
            import sys as _sys
            _sys_stdout = _sys.stdout
            _sys_stderr = _sys.stderr
            _sys.stdout = utils.StreamToLogger(job_logger, level=logging.INFO)
            _sys.stderr = utils.StreamToLogger(job_logger, level=logging.ERROR)
            try:
                executor.run(triggered_by='subprocess', job_id=args.job_id)
            finally:
                try:
                    _sys.stdout.flush()
                except Exception:
                    pass
                try:
                    _sys.stderr.flush()
                except Exception:
                    pass
                _sys.stdout = _sys_stdout
                _sys.stderr = _sys_stderr
                try:
                    if handler:
                        handler.flush()
                except Exception:
                    pass
        else:
            # Fallback: write directly to the log file
            with open(log_path, 'a', encoding='utf-8', errors='replace') as fh:
                with contextlib.redirect_stdout(fh), contextlib.redirect_stderr(fh):
                    executor.run(triggered_by='subprocess', job_id=args.job_id)
    except Exception as e:
        # Final fallback: ensure errors are visible
        try:
            with open(log_path, 'a', encoding='utf-8', errors='replace') as fh:
                fh.write((str(e) + '\n'))
        except Exception:
            pass
        logger.exception("Job run for archive id=%s failed", args.archive_id)
        raise

if __name__ == '__main__':
    main()

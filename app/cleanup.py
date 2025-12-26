"""
Cleanup tasks for orphaned files and old data.
"""
import os
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from app.db import get_db
from app.notifications.helpers import get_setting, get_subject_with_tag
from app.notifications.formatters import strip_html_tags
from app.notifications.helpers import get_user_emails
from app import utils
from app.utils import setup_logging, get_logger, format_bytes

# Configure logging using centralized setup so LOG_LEVEL is respected
setup_logging()
logger = get_logger(__name__)


ARCHIVE_BASE = utils.get_archives_path()


def run_cleanup(dry_run_override=None, job_id=None, tasks=None):
    """Run all cleanup tasks.

    If `dry_run_override` is provided (True/False), it overrides the configured
    `cleanup_dry_run` setting for this invocation.
    """
    # Check if cleanup is enabled
    enabled = get_setting('cleanup_enabled', 'true').lower() == 'true'
    if not enabled:
        logger.info("[Cleanup] Cleanup task is disabled in settings")
        return
    
    is_dry_run = get_setting('cleanup_dry_run', 'false').lower() == 'true'
    if dry_run_override is not None:
        is_dry_run = bool(dry_run_override)

    log_retention_days = int(get_setting('cleanup_log_retention_days', '90'))
    notify_cleanup = get_setting('notify_on_cleanup', 'false').lower() == 'true'
    
    mode = "DRY RUN" if is_dry_run else "LIVE"
    start_time = utils.now()
    
    # Create job record if not provided by the caller
    job_id = job_id
    log_lines = []
    
    def log_message(level, message):
        timestamp = utils.local_now().strftime('%Y-%m-%d %H:%M:%S')
        log_line = f"[{timestamp}] [{level}] {message}\n"
        log_lines.append(log_line)
        # Also write into the per-cleanup logfile when available
        if job_log_fh:
            try:
                job_log_fh.write(log_line)
                try:
                    job_log_fh.flush()
                except Exception:
                    pass
            except Exception:
                logger.exception("[Cleanup] Failed to write to cleanup job log file")
        if level == 'ERROR':
            logger.error("[Cleanup] %s", message)
        elif level == 'WARNING':
            logger.warning("[Cleanup] %s", message)
        else:
            logger.info("[Cleanup] %s", message)
    
    # Create a DB job record if not provided by the caller. We also create a
    # per-run cleanup log file at LOG_DIR/jobs/cleanup_<jobid>.log (or a
    # timestamped fallback when no job_id is available).
    if not job_id:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO jobs (job_type, status, start_time, triggered_by, is_dry_run, log)
                    VALUES ('cleanup', 'running', %s, 'scheduled', %s, '')
                    RETURNING id;
                """, (start_time, is_dry_run))
                job_id = cur.fetchone()['id']
                conn.commit()
        except Exception as e:
            logger.exception("[Cleanup] Failed to create job record: %s", e)
            # Continue anyway

    # Prepare a per-cleanup logfile (persisted under LOG_DIR/jobs)
    job_log_fh = None
    job_log_path = None
    try:
        jobs_dir = os.path.join(utils.get_log_dir(), 'jobs')
        os.makedirs(jobs_dir, exist_ok=True)
        # Use UTC timestamped filenames for cleanup logs (include job_id when available)
        ts = utils.filename_timestamp()
        if job_id:
            job_log_path = os.path.join(jobs_dir, f"cleanup_{job_id}_{ts}.log")
        else:
            job_log_path = os.path.join(jobs_dir, f"cleanup_{ts}.log")
        job_log_fh = open(job_log_path, 'a', encoding='utf-8')
    except Exception as e:
        logger.exception("[Cleanup] Failed to create cleanup job log file: %s", e)
        job_log_fh = None

    # Rotating cleanup summary was removed: prefer per-job logs under LOG_DIR/jobs/
    summary_logger = None
    
    log_message('INFO', f"Starting cleanup task ({mode})")
    
    # Run cleanup tasks and collect stats
    try:
        # Determine which tasks to run
        run_all = tasks is None
        task_set = set(tasks) if isinstance(tasks, list) else set()

        if run_all or 'orphaned_archives' in task_set:
            orphaned_stats = cleanup_orphaned_archives(is_dry_run, log_message)
        else:
            orphaned_stats = {'count': 0, 'reclaimed': 0}

        if run_all or 'old_logs' in task_set:
            log_stats = cleanup_old_logs(log_retention_days, is_dry_run, log_message)
        else:
            log_stats = {'count': 0, 'log_files_deleted': 0, 'deleted_files': []}

        if run_all or 'unreferenced_dirs' in task_set:
            unreferenced_dirs_stats = cleanup_unreferenced_dirs(is_dry_run, log_message)
        else:
            unreferenced_dirs_stats = {'count': 0, 'reclaimed': 0}

        total_reclaimed = orphaned_stats.get('reclaimed', 0) + unreferenced_dirs_stats.get('reclaimed', 0)

        # Handle unreferenced files: list in dry-run or delete in live run
        try:
            if run_all or 'unreferenced_files' in task_set:
                uf_stats = cleanup_unreferenced_files(is_dry_run, log_message)
                # uf_stats: {'count': total_candidates, 'deleted': deleted_count, 'reclaimed': bytes}
                total_reclaimed += uf_stats.get('reclaimed', 0)
            else:
                uf_stats = {'count': 0, 'deleted': 0, 'reclaimed': 0}
        except Exception as e:
            log_message('ERROR', f'Failed to process unreferenced files: {e}')

        # Download tokens cleanup (remove expired tokens and files)
        # Download tokens cleanup (remove expired tokens and files) if requested
        try:
            if run_all or 'download_tokens' in task_set:
                log_message('INFO', 'Running download token cleanup')
                stats = cleanup_download_tokens(is_dry_run=is_dry_run, log_callback=log_message)
                log_message('INFO', f"Download cleanup: deleted_tokens={stats.get('deleted_tokens', 0)}, deleted_files={stats.get('deleted_files', 0)}, reclaimed={format_bytes(stats.get('reclaimed_bytes', 0))}")
            else:
                stats = {'deleted_tokens': 0, 'deleted_files': 0, 'reclaimed_bytes': 0}
        except Exception as e:
            log_message('WARNING', f'Failed to cleanup download tokens: {e}')
            stats = {'deleted_tokens': 0, 'deleted_files': 0, 'reclaimed_bytes': 0}

        log_message('INFO', f"Cleanup task completed ({mode})")
        log_message('INFO', f"Total reclaimed: {format_bytes(total_reclaimed)}")

        # Global summary for all checks
        try:
            orphaned_count = orphaned_stats.get('count', 0)
            orphaned_reclaimed = orphaned_stats.get('reclaimed', 0)
            unreferenced_dirs_count = unreferenced_dirs_stats.get('count', 0)
            unreferenced_dirs_reclaimed = unreferenced_dirs_stats.get('reclaimed', 0)
            old_logs = log_stats.get('count', 0)
            unref_count = uf_stats.get('count', 0)
            unref_reclaimed = uf_stats.get('reclaimed', 0)

            summary = (
                f"Summary: Orphaned Archives: {orphaned_count} ({format_bytes(orphaned_reclaimed)}); "
                f"Unreferenced files: {unref_count} ({format_bytes(unref_reclaimed)}); "
                f"Unreferenced Directories: {unreferenced_dirs_count} ({format_bytes(unreferenced_dirs_reclaimed)}); "
                f"Old Logs: {old_logs}; Total Reclaimed: {format_bytes(total_reclaimed)}"
            )
            log_message('INFO', summary)
        except Exception:
            # If any stat is unavailable, skip the consolidated summary
            pass
        
        # Update job status
        if job_id:
            with get_db() as conn:
                cur = conn.cursor()
                end_time = utils.now()
                cur.execute("""
                    UPDATE jobs 
                    SET status = 'success', end_time = %s, 
                        reclaimed_size_bytes = %s, log = %s
                    WHERE id = %s;
                """, (end_time, total_reclaimed, ''.join(log_lines), job_id))
                conn.commit()
        
        # Send notification if enabled
        if notify_cleanup:
            send_cleanup_notification(orphaned_stats, log_stats, unreferenced_dirs_stats, uf_stats, total_reclaimed, is_dry_run, download_stats=stats, job_id=job_id)


            
    except Exception as e:
        log_message('ERROR', f"Cleanup failed: {str(e)}")
        
        if job_id:
            with get_db() as conn:
                cur = conn.cursor()
                end_time = utils.now()
                cur.execute("""
                    UPDATE jobs 
                    SET status = 'failed', end_time = %s, 
                        error_message = %s, log = %s
                    WHERE id = %s;
                """, (end_time, str(e), ''.join(log_lines), job_id))
                conn.commit()



        # Ensure the per-cleanup logfile is closed
        try:
            if job_log_fh:
                job_log_fh.close()
        except Exception:
            pass
        
        raise


def cleanup_orphaned_archives(is_dry_run=False, log_callback=None):
    """Remove archive directories that no longer have a database entry."""
    def log(message):
        if log_callback:
            log_callback('INFO', message)
        else:
            logger.info("[Cleanup] %s", message)
    
    log("Checking for orphaned archive directories...")
    
    archive_base = Path(ARCHIVE_BASE)
    if not archive_base.exists():
        log("Archive directory does not exist, skipping")
        return {'count': 0, 'reclaimed': 0}
    
    # Get all archive names from database
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM archives;")
        db_archives = {row['name'] for row in cur.fetchall()}
    
    # Check filesystem directories
    orphaned_count = 0
    reclaimed_bytes = 0
    
    for archive_dir in archive_base.iterdir():
        if not archive_dir.is_dir():
            continue
        
        # Skip special directories
        if archive_dir.name.startswith('_'):
            continue
        
        # Check if archive exists in database
        if archive_dir.name not in db_archives:
            size = get_directory_size(archive_dir)
            reclaimed_bytes += size
            orphaned_count += 1
            
            if is_dry_run:
                log(f"Would delete orphaned archive directory: {archive_dir.name} ({format_bytes(size)})")
            else:
                log(f"Deleting orphaned archive directory: {archive_dir.name} ({format_bytes(size)})")
                # Mark all archives in this directory as deleted
                _mark_archives_as_deleted_by_path(str(archive_dir), 'cleanup')
                try:
                    shutil.rmtree(archive_dir)
                except Exception as e:
                    # Log and continue with other directories; don't let one failure abort the entire cleanup
                    log(f"Failed to delete {archive_dir}: {e}")
        else:
            # Archive directory exists in DB — detailed per-file inspection is handled by the
            # dedicated `cleanup_unreferenced_files` pass to avoid duplicate logging.
            log(f"Archive directory {archive_dir.name} exists in DB; detailed file checks are handled separately")
    
    if orphaned_count > 0:
        log(f"Found {orphaned_count} orphaned archive(s), {format_bytes(reclaimed_bytes)} to reclaim")
    else:
        log("No orphaned archives found")
    
    return {'count': orphaned_count, 'reclaimed': reclaimed_bytes}


def cleanup_download_tokens(is_dry_run=False, log_callback=None):
    """Cleanup expired download tokens and their temporary files.

    This only deletes files that are located inside the application's
    temporary downloads directory (get_downloads_path()).

    Returns a stats dict: {'deleted_tokens': int, 'deleted_files': int}
    """
    def log(level, message):
        if log_callback:
            log_callback(level, message)
        else:
            if level == 'ERROR':
                logger.error("[Cleanup] %s", message)
            elif level == 'WARNING':
                logger.warning("[Cleanup] %s", message)
            else:
                logger.info("[Cleanup] %s", message)

    deleted_tokens = 0
    deleted_files = 0
    reclaimed_bytes = 0
    downloads_base = utils.get_downloads_path()

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT token, file_path FROM download_tokens WHERE expires_at < NOW();")
            rows = cur.fetchall()

            for row in rows:
                token = row.get('token')
                fp = row.get('file_path')

                if not fp:
                    # No file path set, delete token directly (or count for dry-run)
                    if not is_dry_run:
                        cur.execute("DELETE FROM download_tokens WHERE token = %s;", (token,))
                    deleted_tokens += 1
                    log('INFO', f"Removing expired token (no file): {token}")
                    continue

                p = Path(fp)
                try:
                    # Safely resolve and ensure the file sits under the downloads directory
                    downloads_base_resolved = downloads_base.resolve()
                    p_resolved = p.resolve()
                    if downloads_base_resolved in p_resolved.parents or p_resolved == downloads_base_resolved:
                        if p_resolved.exists() and p_resolved.is_file():
                            try:
                                file_size = p_resolved.stat().st_size
                            except Exception:
                                file_size = 0

                            if is_dry_run:
                                deleted_files += 1
                                deleted_tokens += 1
                                reclaimed_bytes += file_size
                                log('INFO', f"Would delete download file: {p_resolved} ({format_bytes(file_size)})")
                            else:
                                try:
                                    p_resolved.unlink()
                                    cur.execute("DELETE FROM download_tokens WHERE token = %s;", (token,))
                                    deleted_files += 1
                                    deleted_tokens += 1
                                    reclaimed_bytes += file_size
                                    log('INFO', f"Deleted download file and token: {p_resolved} (token={token}) reclaimed={format_bytes(file_size)}")
                                except Exception as e:
                                    log('WARNING', f"Failed to delete file {p_resolved}: {e}")
                        else:
                            # File missing - delete token only
                            if is_dry_run:
                                deleted_tokens += 1
                                log('INFO', f"Would remove token for missing file: {token}")
                            else:
                                cur.execute("DELETE FROM download_tokens WHERE token = %s;", (token,))
                                deleted_tokens += 1
                                log('INFO', f"Removed token for missing file: {token}")
                    else:
                        log('DEBUG', f"Skipping token {token} - file outside downloads dir: {p}")
                except Exception as e:
                    log('WARNING', f"Failed to evaluate path {fp} for token {token}: {e}")

            if not is_dry_run:
                conn.commit()

    except Exception as e:
        log('ERROR', f"Error cleaning up download tokens: {e}")

    return {'deleted_tokens': deleted_tokens, 'deleted_files': deleted_files, 'reclaimed_bytes': reclaimed_bytes}


def cleanup_old_logs(retention_days, is_dry_run=False, log_callback=None):
    """Delete old job records from database and rotated log files.

    Returns a dict with keys: 'count' (DB rows deleted), 'log_files_deleted' (int),
    and 'deleted_files' (list of file paths removed).
    """
    def log(message):
        if log_callback:
            log_callback('INFO', message)
        else:
            logger.info("[Cleanup] %s", message)
    
    if retention_days <= 0:
        log("Log retention disabled (retention_days <= 0)")
        return {'count': 0, 'log_files_deleted': 0, 'deleted_files': []}
    
    log(f"Checking for logs older than {retention_days} days...")
    import time
    import os

    with get_db() as conn:
        cur = conn.cursor()
        # Count jobs to delete
        cur.execute("""
            SELECT COUNT(*) as count 
            FROM jobs 
            WHERE start_time < NOW() - INTERVAL '%s days';
        """, (retention_days,))
        count = cur.fetchone()['count']

        if count == 0:
            log("No old logs to delete")
            # Still run file cleanup for completeness
            try:
                log_dir = utils.get_log_dir()
                result = cleanup_rotated_log_files(retention_days, log_dir, is_dry_run, log_callback=log_callback)
                return {'count': 0, 'log_files_deleted': result.get('log_files_deleted', 0), 'deleted_files': result.get('deleted_files', [])}
            except Exception as e:
                log(f"Exception while cleaning log files: {e}")
                return {'count': 0, 'log_files_deleted': 0, 'deleted_files': []}

        if is_dry_run:
            log(f"Would delete {count} old job record(s)")
        else:
            cur.execute("""
                DELETE FROM jobs 
                WHERE start_time < NOW() - INTERVAL '%s days';
            """, (retention_days,))
            conn.commit()
            log(f"Deleted {count} old job record(s)")

    # Delegate rotated log file cleanup to a dedicated, testable function
    log_files_deleted = 0
    deleted_files = []
    try:
        log_dir = utils.get_log_dir()
        result = cleanup_rotated_log_files(retention_days, log_dir, is_dry_run, log_callback=log_callback)
        log_files_deleted = result.get('log_files_deleted', 0)
        deleted_files = result.get('deleted_files', [])
    except Exception as e:
        log(f"Exception while cleaning log files: {e}")

    return {'count': count, 'log_files_deleted': log_files_deleted, 'deleted_files': deleted_files}


def cleanup_rotated_log_files(retention_days, log_dir, is_dry_run=False, log_callback=None):
    """Delete rotated log files older than `retention_days` in `log_dir`.

    This function is standalone and testable. It reports actions via an optional
    `log_callback` (callable accepting level and message) so callers can capture
    messages into the cleanup job log. Returns a dict: {
      'log_files_deleted': int,
      'deleted_files': [<paths>]
    }
    Signature: cleanup_rotated_log_files(retention_days, log_dir, is_dry_run, log_callback=None)
    """
    import time
    import os

    def log(message, level='INFO'):
        if log_callback:
            try:
                log_callback(level, message)
            except Exception:
                logger.exception("[Cleanup] log_callback raised an exception")
        else:
            if level == 'ERROR':
                logger.error("[Cleanup] %s", message)
            elif level == 'WARNING':
                logger.warning("[Cleanup] %s", message)
            else:
                logger.info("[Cleanup] %s", message)

    log_files_deleted = 0
    deleted_files = []

    if retention_days <= 0:
        log("Log file retention disabled (retention_days <= 0)")
        return {'log_files_deleted': 0, 'deleted_files': []}

    cutoff = int(retention_days) * 86400

    try:
        if os.path.isdir(log_dir):
            log(f"Checking for log files in {log_dir} older than {retention_days} days...")
            log_file_name = getattr(utils, 'LOG_FILE_NAME', 'app.log')
            # Scan top-level log files (app log rotations)
            # Scan top-level log files (app log rotations)
            for fn in os.listdir(log_dir):
                # Match files that start with the configured log filename
                if not fn.startswith(log_file_name):
                    continue
                fp = os.path.join(log_dir, fn)
                try:
                    # Skip the active app log file (exact match)
                    if fn == log_file_name:
                        continue
                    mtime = os.path.getmtime(fp)
                    age = time.time() - mtime
                    if age > cutoff:
                        if is_dry_run:
                            log(f"Would delete old log file: {fp}")
                        else:
                            try:
                                os.unlink(fp)
                                log_files_deleted += 1
                                deleted_files.append(fp)
                                log(f"Deleted old log file: {fp}")
                            except Exception as e:
                                log(f"Failed to delete log file {fp}: {e}", level='WARNING')
                except Exception:
                    # Best-effort: ignore files we can't stat
                    continue

            # Also scan job-specific logs under LOG_DIR/jobs
            jobs_dir = os.path.join(log_dir, 'jobs')
            try:
                if os.path.isdir(jobs_dir):
                    for root, _, files in os.walk(jobs_dir):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            try:
                                mtime = os.path.getmtime(fp)
                                age = time.time() - mtime
                                if age > cutoff:
                                    if is_dry_run:
                                        log(f"Would delete old job log file: {fp}")
                                    else:
                                        try:
                                            os.unlink(fp)
                                            log_files_deleted += 1
                                            deleted_files.append(fp)
                                            log(f"Deleted old job log file: {fp}")
                                        except Exception as e:
                                            log(f"Failed to delete job log file {fp}: {e}", level='WARNING')
                            except Exception:
                                continue
            except Exception:
                # Best-effort: ignore
                pass
        else:
            log(f"Log directory {log_dir} does not exist; skipping file cleanup")
    except Exception as e:
        log(f"Exception while cleaning log files: {e}", level='ERROR')

    return {'log_files_deleted': log_files_deleted, 'deleted_files': deleted_files}


def cleanup_unreferenced_dirs(is_dry_run=False, log_callback=None):
    """Detect and optionally delete unreferenced empty stack directories.

    This function scans archive directories for stack directories that contain
    no valid backups (per `is_stack_directory_empty`) and have no active DB
    references. It reports candidates in dry-run mode and deletes them in live
    mode, returning stats {'count': int, 'reclaimed': int}.
    """
    def log(message):
        if log_callback:
            log_callback('INFO', message)
        else:
            logger.info("[Cleanup] %s", message)
    
    log("Checking for unreferenced directories...")
    
    archive_base = Path(ARCHIVE_BASE)
    if not archive_base.exists():
        return {'count': 0, 'reclaimed': 0}
    
    dir_count = 0
    reclaimed_bytes = 0
    
    
    # Find empty stack directories
    for archive_dir in archive_base.iterdir():
        if not archive_dir.is_dir() or archive_dir.name.startswith('_'):
            continue

        for stack_dir in archive_dir.iterdir():
            if not stack_dir.is_dir():
                continue

            # Check if stack directory is empty or has no valid backups
            if is_stack_directory_empty(stack_dir, log_callback=log):
                # Before removing, ensure there are no active DB references to archives under this path
                try:
                    with get_db() as conn:
                        cur = conn.cursor()
                        prefix = str(stack_dir)
                        like_pattern = prefix + '/%'
                        cur.execute("SELECT 1 FROM job_stack_metrics WHERE (archive_path = %s OR archive_path LIKE %s) AND deleted_at IS NULL LIMIT 1;", (prefix, like_pattern))
                        active = cur.fetchone()
                except Exception as e:
                    # If DB check fails, be conservative and skip deletion; log the error
                    log(f"DB check failed for {stack_dir.relative_to(archive_base)}: {e}. Skipping deletion.")
                    active = True

                if active:
                    # Skip deletion if an active DB reference exists
                    if is_dry_run:
                        log(f"Would keep stack directory (active DB references exist): {str(stack_dir)}")
                    else:
                        log(f"Skipping deletion (active DB references exist): {str(stack_dir)}")
                    continue

                dir_count += 1

                # Attempt to fetch a recent job reference for context (even if deleted) so logs show source
                reference_info = ''
                archive_label = None
                try:
                    with get_db() as conn:
                        cur = conn.cursor()
                        like = str(stack_dir) + '/%'
                        cur.execute("""
                            SELECT j.id as job_id, a.name as archive_name, m.stack_name
                            FROM job_stack_metrics m
                            LEFT JOIN jobs j ON m.job_id = j.id
                            LEFT JOIN archives a ON j.archive_id = a.id
                            WHERE m.archive_path LIKE %s
                            ORDER BY j.start_time DESC NULLS LAST LIMIT 1;
                        """, (like,))
                        ref = cur.fetchone()
                        if ref:
                            reference_info = f" (archive '{ref.get('archive_name') or 'unknown'}', stack '{ref.get('stack_name') or 'unknown'}')"
                            archive_label = ref.get('archive_name')
                except Exception as e:
                    # Non-fatal; include note in logs
                    reference_info = f" (DB lookup failed: {e})"

                # Determine display path to include archive name when available
                rel = str(stack_dir.relative_to(archive_base))
                if archive_label:
                    # Avoid duplicating the archive name if it's already present at the start
                    if rel.startswith(f"{archive_label}/") or rel == archive_label:
                        display_path = rel
                    else:
                        display_path = f"{archive_label}/{rel}"
                else:
                    # Fallback: infer archive name from path (parent directory)
                    try:
                        inferred = stack_dir.parent.name
                        if rel.startswith(f"{inferred}/") or rel == inferred:
                            display_path = rel
                        else:
                            display_path = f"{inferred}/{rel}"
                    except Exception:
                        display_path = rel

                # Compute reclaimable size for clearer reporting
                try:
                    dir_size = get_directory_size(stack_dir)
                except Exception:
                    dir_size = 0

                if is_dry_run:
                    log(f"Would delete unreferenced directory: {str(stack_dir)}{reference_info} — no DB references / no archive files found (would reclaim {format_bytes(dir_size)})")
                else:
                    log(f"Deleting unreferenced directory: {str(stack_dir)}{reference_info} — removing (reclaimed {format_bytes(dir_size)})")
                    try:
                        shutil.rmtree(stack_dir)
                        # Account for reclaimed bytes from directory removal
                        reclaimed_bytes += dir_size
                    except Exception as e:
                        log(f"Failed to delete unreferenced directory {str(stack_dir)}: {e}")
    if dir_count > 0:
        log(f"Found {dir_count} unreferenced directory(ies), {format_bytes(reclaimed_bytes)} to reclaim")
    else:
        log("No unreferenced directories found")
    
    return {'count': dir_count, 'reclaimed': reclaimed_bytes}


def is_stack_directory_empty(stack_dir, log_callback=None):
    """Check if a stack directory has no valid backups.

    Heuristics used:
      - If any archive file (.tar, .tar.gz, .tar.zst) exists anywhere under the
        directory, it's considered non-empty.
      - If any subdirectory name starts with a timestamp pattern (YYYYMMDD_HHMMSS),
        optionally followed by suffix (e.g. "20251221_182125_beszel"), it's
        considered non-empty.
      - If there's a nested directory with the same stack name that contains files,
        it's considered non-empty (covers layouts like stack/stack/...).
    """
    import re

    # Completely empty
    try:
        if not any(stack_dir.iterdir()):
            return True
    except Exception:
        # If something goes wrong reading dir, treat it as non-empty to be safe
        return False

    # Before scanning files, check whether this directory (or anything under it)
    # is referenced in the DB. If any active DB reference exists, we must treat
    # the directory as non-empty and stop immediately.
    try:
        prefix = str(stack_dir) + '/%'
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM job_stack_metrics WHERE (archive_path = %s OR archive_path LIKE %s) AND deleted_at IS NULL LIMIT 1;", (str(stack_dir), prefix))
            rref = cur.fetchone()
            if rref:
                # Directory is referenced in DB — not empty
                if log_callback:
                    try:
                        log_callback(f"Directory has active DB reference: {stack_dir}")
                    except Exception:
                        pass
                return False
    except Exception:
        # If DB check fails, be conservative
        return False

    # 1) If any archive-like file exists anywhere under this stack_dir, check DB
    #    whether it's a referenced archive. If a referenced archive file is found,
    #    consider the directory non-empty. Unreferenced archive files are noted
    #    (via log_callback) but do not prevent the directory from being treated
    #    as empty for cleanup.
    archive_exts = ('.tar', '.tar.gz', '.tgz', '.tar.zst', '.zst', '.zip')

    found_unreferenced_archive = False
    for f in stack_dir.rglob('*'):
        try:
            if f.is_file():
                name = f.name.lower()
                if any(name.endswith(ext) for ext in archive_exts):
                    # Check DB for a reference to this archive file
                    try:
                        with get_db() as conn:
                            cur = conn.cursor()
                            cur.execute("SELECT 1 FROM job_stack_metrics WHERE archive_path = %s OR archive_path LIKE %s LIMIT 1;", (str(f), f"%/{f.name}"))
                            r = cur.fetchone()
                            if r:
                                # Referenced archive found -> directory is non-empty
                                return False
                            else:
                                # Unreferenced archive file: note and continue scanning
                                found_unreferenced_archive = True
                                if log_callback:
                                    try:
                                        log_callback(f"Found unreferenced archive file: {f.relative_to(stack_dir)}")
                                    except Exception:
                                        # Best-effort logging
                                        pass
                    except Exception:
                        # If DB check fails, be conservative and treat as non-empty
                        return False
        except Exception:
            continue

    # If no referenced archive files exist, or only small non-archive files/unreferenced
    # archive files were found, we still consider timestamped subdirectories or nested
    # stack directories as indicators of non-empty backup folders.

    # 2) Check for timestamp-like subdirectories (timestamp at start of name)
    timestamp_re = re.compile(r'^\d{8}_\d{6}')
    for d in stack_dir.iterdir():
        if d.is_dir() and timestamp_re.match(d.name):
            return False

    # 3) Nested stack directory (e.g., stack/stack) that contains files
    for d in stack_dir.iterdir():
        if d.is_dir() and d.name == stack_dir.name:
            # if nested dir contains at least one file, consider non-empty
            try:
                if any(p.is_file() for p in d.rglob('*')):
                    return False
            except Exception:
                # Be conservative
                return False

    # If none of the above heuristics matched, consider it empty
    return True


def is_valid_timestamp_dirname(dirname):
    """Check if directory name matches timestamp pattern YYYYMMDD_HHMMSS.

    The project uses the compact format (YYYYMMDD_HHMMSS) for timestamped
    folders; only this canonical format is accepted here.
    """
    try:
        from re import match
        return bool(match(r"^\d{8}_\d{6}$", dirname))
    except Exception:
        return False


def get_directory_size(path):
    """Calculate total size of directory."""
    total = 0
    try:
        for entry in Path(path).rglob('*'):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception as e:
        logger.exception("[Cleanup] Error calculating size for %s: %s", path, e)
    return total





def _mark_archives_as_deleted_by_path(path_prefix, deleted_by='cleanup'):
    """Mark all archives under a path as deleted in database."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            now_ts = utils.now()
            cur.execute("""
                UPDATE job_stack_metrics 
                SET deleted_at = %s, deleted_by = %s
                WHERE archive_path LIKE %s AND deleted_at IS NULL;
            """, (now_ts, deleted_by, f"{path_prefix}%"))
            conn.commit()
    except Exception as e:
        logger.exception("[Cleanup] Failed to mark archives as deleted in DB: %s", e)


def cleanup_unreferenced_files(is_dry_run=False, log_callback=None):
    """Find and (optionally) delete files inside archive dirs that are not referenced in DB.

    Returns a dict: {'count': int, 'reclaimed': int}
    """
    def log(message):
        if log_callback:
            log_callback('INFO', message)
        else:
            logger.info("[Cleanup] %s", message)

    archive_base = Path(ARCHIVE_BASE)
    if not archive_base.exists():
        return {'count': 0, 'reclaimed': 0}

    # Header for consistency with other checks
    log("Checking for unreferenced files...")

    deleted_count = 0
    reclaimed = 0
    unreferenced_count = 0
    potential_unreferenced_reclaimed = 0

    # Iterate archive dirs that are known in DB
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM archives;")
        db_archives = {row['name'] for row in cur.fetchall()}

    for archive_dir in archive_base.iterdir():
        if not archive_dir.is_dir() or archive_dir.name.startswith('_'):
            continue
        if archive_dir.name not in db_archives:
            continue

        # Collect unreferenced files for this archive dir
        unreferenced_files = []
        for entry in archive_dir.iterdir():
            try:
                if entry.is_file():
                    # Check DB references for this file
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("SELECT 1 FROM job_stack_metrics WHERE archive_path = %s OR archive_path LIKE %s LIMIT 1;", (str(entry), f"%/{entry.name}"))
                        r = cur.fetchone()
                        if not r:
                            size = entry.stat().st_size if entry.exists() else 0
                            unreferenced_files.append({'archive': archive_dir.name, 'path': entry, 'size': size})
                            unreferenced_count += 1
                            potential_unreferenced_reclaimed += size
            except Exception as e:
                log(f"Error inspecting {entry}: {e}")

        # Report or delete unreferenced files for this archive dir
        if is_dry_run:
            if unreferenced_files:
                log(f"Unreferenced files in {str(archive_dir)}: {len(unreferenced_files)}")
                for uf in unreferenced_files:
                    log(f"Unreferenced file: {str(uf['path'])} (no DB reference)")
        else:
            for uf in unreferenced_files:
                path = uf['path']
                try:
                    # Re-check DB before deleting
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("SELECT 1 FROM job_stack_metrics WHERE archive_path = %s OR archive_path LIKE %s LIMIT 1;", (str(path), f"%/{path.name}"))
                        r = cur.fetchone()
                        if not r:
                            try:
                                size = uf['size']
                                path.unlink()
                                deleted_count += 1
                                reclaimed += size
                                log(f"Deleted unreferenced file: {str(path)} ({format_bytes(size)})")
                            except Exception as de:
                                log(f"Failed to delete unreferenced file {path}: {de}")
                        else:
                            log(f"Skipping deletion, file now referenced: {str(path)}")
                except Exception as dbe:
                    log(f"DB check failed before deleting {path}: {dbe}")

    # Per-check summary
    if unreferenced_count == 0:
        log("No unreferenced files found")
    else:
        if is_dry_run:
            log(f"Found {unreferenced_count} unreferenced file(s), {format_bytes(potential_unreferenced_reclaimed)} to reclaim")
        else:
            log(f"Found {deleted_count} unreferenced file(s) deleted, {format_bytes(reclaimed)} reclaimed (candidates: {unreferenced_count})")

    # Return aggregated stats
    return {'count': unreferenced_count, 'deleted': deleted_count if not is_dry_run else 0, 'reclaimed': (reclaimed if not is_dry_run else potential_unreferenced_reclaimed)}


# remove generate_cleanup_report and CLI usage - report not needed per config



def send_cleanup_notification(orphaned_stats, log_stats, unreferenced_dirs_stats, uf_stats, total_reclaimed, is_dry_run, download_stats=None, job_id=None):
    """Send notification about cleanup results. Includes job log when available."""
    try:
        # Debug log to assist with dry-run notification troubleshooting
        logger.info("[Cleanup] Sending cleanup notification (%s)", 'DRY RUN' if is_dry_run else 'LIVE')

        mode = "🧪 DRY RUN" if is_dry_run else "✅"
        
        # Build message
        title = get_subject_with_tag(f"{mode} Cleanup Task Completed")
        
        base_url = get_setting('base_url', 'http://localhost:8080')
        
        body = f"""
<h2>{mode} Cleanup Task</h2>

<h3>Summary</h3>
<ul>
    <li><strong>Orphaned Archives:</strong> {orphaned_stats.get('count', 0)} removed ({format_bytes(orphaned_stats.get('reclaimed', 0))})</li>
    <li><strong>Unreferenced files total:</strong> {uf_stats.get('count', 0)} ({format_bytes(uf_stats.get('reclaimed', 0))})</li>
    <li><strong>Unreferenced Directories:</strong> {unreferenced_dirs_stats.get('count', 0)} removed ({format_bytes(unreferenced_dirs_stats.get('reclaimed', 0))})</li>
    <li><strong>Old Logs:</strong> {log_stats.get('count', 0)} deleted</li>
    <li><strong>Download tokens removed:</strong> {download_stats.get('deleted_tokens', 0) if download_stats else 0} tokens, {download_stats.get('deleted_files', 0) if download_stats else 0} files ({format_bytes(download_stats.get('reclaimed_bytes', 0)) if download_stats else '0 B'})</li>
    <li><strong>Total Reclaimed:</strong> {format_bytes(total_reclaimed)}</li>
</ul>
"""

        # Send via SMTP
        try:
            from app.notifications.adapters import SMTPAdapter
            smtp_adapter = SMTPAdapter() if get_setting('smtp_server') else None
            if not smtp_adapter:
                logger.info("[Cleanup] SMTP not configured; skipping notification")
                return
            res = smtp_adapter.send(title, body, body_format=None, attach=None, recipients=get_user_emails(), context='cleanup')
            if not res.success:
                logger.error("[Cleanup] SMTP send failed: %s", res.detail)
        except Exception as e:
            logger.exception("Failed to send cleanup notification: %s", e)
        # If the cleanup removed individual log files, append a concise list
        deleted_files = log_stats.get('deleted_files', []) or []
        if deleted_files:
            # Optionally group deleted job logs by archive for clearer notifications
            group_logs = get_setting('notify_group_deleted_logs', 'false').lower() == 'true'

            if group_logs:
                # Group by archive id and name when possible. Expected filename format:
                # <archive_id>_<archive_name>.log (possibly with rotation suffixes)
                groups = {}
                others = []
                import re
                for p in deleted_files:
                    base = os.path.basename(p)
                    m = re.match(r'^(?P<archive_id>\d+)_(?P<rest>.+)$', base)
                    if m:
                        aid = m.group('archive_id')
                        rest = m.group('rest')
                        # strip rotation suffixes and file extensions
                        name = rest.split('.')[0]
                        # strip common prefixes like 'dryrun_'
                        name = name.replace('dryrun_', '').replace('scheduled_', '')
                        key = f"{aid}_{name}"
                        groups.setdefault(key, []).append(p)
                    else:
                        others.append(p)

                body += "\n<h3>Deleted Log Files (grouped by archive)</h3>\n"
                total_shown = 0
                for key, files in sorted(groups.items(), key=lambda kv: kv[0]):
                    if total_shown >= 50:
                        break
                    aid, aname = key.split('_', 1)
                    body += f"\n<h4>Archive {aid} — {aname} ({len(files)})</h4>\n<ul>"
                    for p in files:
                        if total_shown >= 50:
                            break
                        body += f"\n  <li>{p}</li>"
                        total_shown += 1
                    if len(files) > 0:
                        body += "\n</ul>"
                # If any ungrouped files remain, show them under 'Other'
                if others and total_shown < 50:
                    body += "\n<h4>Other deleted files</h4>\n<ul>"
                    for p in others[:max(0, 50 - total_shown)]:
                        body += f"\n  <li>{p}</li>"
                    if len(others) > max(0, 50 - total_shown):
                        body += f"\n  <li>...and {len(others) - (50 - total_shown)} more</li>"
                    body += "\n</ul>"
            else:
                body += "\n<h3>Deleted Log Files</h3>\n<ul>"
                for p in deleted_files[:50]:
                    # limit the list to 50 items to avoid huge messages
                    body += f"\n  <li>{p}</li>"
                if len(deleted_files) > 50:
                    body += f"\n  <li>...and {len(deleted_files)-50} more</li>"
                body += "\n</ul>"

        if is_dry_run:
            body += "\n<p><em>⚠️ This was a dry run - no files were actually deleted.</em></p>"
        
        body += f"""
<hr>
<p><small>Docker Archiver: <a href=\"{base_url}\">{base_url}</a></small></p>"""
        
        # Append full job log if available
        job_log = ''
        if job_id:
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT log FROM jobs WHERE id = %s;", (job_id,))
                    row = cur.fetchone()
                    job_log = row.get('log') if row else ''
            except Exception:
                job_log = ''

        if job_log:
            try:
                import html
                escaped = html.escape(job_log)
                body += "\n<h3>Full Cleanup Log</h3>\n<pre style='white-space:pre-wrap;background:#f8f8f8;padding:8px;border-radius:4px;'>" + escaped + "</pre>\n"
            except Exception:
                # Fallback: attach as plain text
                body += "\n\nFull Cleanup Log:\n" + job_log

        # If a per-cleanup job log file exists, add a quick reference (file path) so
        # recipients know where to download the raw log if needed.
        try:
            jobs_dir = os.path.join(utils.get_log_dir(), 'jobs')
            file_path = None
            if job_id:
                # Find the most recent matching cleanup_{job_id}_*.log (fallback to cleanup_{job_id}.log for older installs)
                import glob
                pattern = os.path.join(jobs_dir, f"cleanup_{job_id}_*.log")
                matches = glob.glob(pattern)
                if matches:
                    file_path = max(matches, key=os.path.getmtime)
                else:
                    legacy = os.path.join(jobs_dir, f"cleanup_{job_id}.log")
                    if os.path.exists(legacy):
                        file_path = legacy
            if file_path and os.path.exists(file_path):
                body += f"\n<p>Full cleanup job log file: <code>{file_path}</code></p>"
        except Exception:
            pass

    except Exception as e:
        logger.exception("[Cleanup] Failed to send notification: %s", e)


if __name__ == '__main__':
    import argparse, json
    parser = argparse.ArgumentParser(description='Cleanup utilities: generate a dry-run cleanup report')
    parser.add_argument('--archive-base', help='Path to archive base (overrides default)')
    parser.add_argument('--json', action='store_true', help='Output JSON')
    parser.add_argument('--run-cleanup', action='store_true', help='Run cleanup now (live mode)')
    parser.add_argument('--dry-run', action='store_true', help='Run cleanup in dry-run mode')
    args = parser.parse_args()

    if args.run_cleanup:
        # Run cleanup (honor --dry-run if provided)
        run_cleanup(dry_run_override=(True if args.dry_run else False))
    else:
        # No generate report functionality; run cleanup with --run-cleanup
        logger.info('No report available. Use --run-cleanup to perform a cleanup (add --dry-run for a dry-run).')


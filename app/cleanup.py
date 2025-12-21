"""
Cleanup tasks for orphaned files and old data.
"""
import os
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from app.db import get_db
from app.notifications import get_setting
from app import utils


ARCHIVE_BASE = '/archives'


def run_cleanup(dry_run_override=None, job_id=None):
    """Run all cleanup tasks.

    If `dry_run_override` is provided (True/False), it overrides the configured
    `cleanup_dry_run` setting for this invocation.
    """
    from app.notifications import get_setting
    from datetime import datetime
    
    # Check if cleanup is enabled
    enabled = get_setting('cleanup_enabled', 'true').lower() == 'true'
    if not enabled:
        print("[Cleanup] Cleanup task is disabled in settings")
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
        print(f"[Cleanup] {message}")
    
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
            print(f"[Cleanup] Failed to create job record: {e}")
            # Continue anyway
    
    log_message('INFO', f"Starting cleanup task ({mode})")
    
    # Run cleanup tasks and collect stats
    try:
        orphaned_stats = cleanup_orphaned_archives(is_dry_run, log_message)
        log_stats = cleanup_old_logs(log_retention_days, is_dry_run, log_message)
        temp_stats = cleanup_unreferenced_dirs(is_dry_run, log_message)
        
        total_reclaimed = orphaned_stats.get('reclaimed', 0) + temp_stats.get('reclaimed', 0)
        
        # Handle unreferenced files: list in dry-run or delete in live run
        try:
            uf_stats = cleanup_unreferenced_files(is_dry_run, log_message)
            # uf_stats: {'count': total_candidates, 'deleted': deleted_count, 'reclaimed': bytes}
            total_reclaimed += uf_stats.get('reclaimed', 0)
        except Exception as e:
            log_message('ERROR', f'Failed to process unreferenced files: {e}')


        log_message('INFO', f"Cleanup task completed ({mode})")
        log_message('INFO', f"Total reclaimed: {format_bytes(total_reclaimed)}")

        # Global summary for all checks
        try:
            orphaned_count = orphaned_stats.get('count', 0)
            orphaned_reclaimed = orphaned_stats.get('reclaimed', 0)
            temp_count = temp_stats.get('count', 0)
            temp_reclaimed = temp_stats.get('reclaimed', 0)
            old_logs = log_stats.get('count', 0)
            unref_count = uf_stats.get('count', 0)
            unref_reclaimed = uf_stats.get('reclaimed', 0)

            summary = (
                f"Summary: Orphaned Archives: {orphaned_count} ({format_bytes(orphaned_reclaimed)}), "
                f"Unreferenced files: {unref_count} ({format_bytes(unref_reclaimed)}), "
                f"Unreferenced Directories: {temp_count} ({format_bytes(temp_reclaimed)}), "
                f"Old Logs: {old_logs}, Total Reclaimed: {format_bytes(total_reclaimed)}"
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
            send_cleanup_notification(orphaned_stats, log_stats, temp_stats, uf_stats, total_reclaimed, is_dry_run, job_id=job_id)
            
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
        
        raise


def cleanup_orphaned_archives(is_dry_run=False, log_callback=None):
    """Remove archive directories that no longer have a database entry."""
    def log(message):
        if log_callback:
            log_callback('INFO', message)
        else:
            print(f"[Cleanup] {message}")
    
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


def cleanup_old_logs(retention_days, is_dry_run=False, log_callback=None):
    """Delete old job records from database."""
    def log(message):
        if log_callback:
            log_callback('INFO', message)
        else:
            print(f"[Cleanup] {message}")
    
    if retention_days <= 0:
        log("Log retention disabled (retention_days <= 0)")
        return {'count': 0}
    
    log(f"Checking for logs older than {retention_days} days...")
    
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
            return {'count': 0}
        
        if is_dry_run:
            log(f"Would delete {count} old job record(s)")
        else:
            cur.execute("""
                DELETE FROM jobs 
                WHERE start_time < NOW() - INTERVAL '%s days';
            """, (retention_days,))
            conn.commit()
            log(f"Deleted {count} old job record(s)")
    
    return {'count': count}


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
            print(f"[Cleanup] {message}")
    
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
        print(f"[Cleanup] Error calculating size for {path}: {e}")
    return total


def format_bytes(size):
    """Format bytes as human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def _mark_archives_as_deleted_by_path(path_prefix, deleted_by='cleanup'):
    """Mark all archives under a path as deleted in database."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE job_stack_metrics 
                SET deleted_at = NOW(), deleted_by = %s
                WHERE archive_path LIKE %s AND deleted_at IS NULL;
            """, (deleted_by, f"{path_prefix}%"))
            conn.commit()
    except Exception as e:
        print(f"[Cleanup] Failed to mark archives as deleted in DB: {e}")


def cleanup_unreferenced_files(is_dry_run=False, log_callback=None):
    """Find and (optionally) delete files inside archive dirs that are not referenced in DB.

    Returns a dict: {'count': int, 'reclaimed': int}
    """
    def log(message):
        if log_callback:
            log_callback('INFO', message)
        else:
            print(f"[Cleanup] {message}")

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



def send_cleanup_notification(orphaned_stats, log_stats, temp_stats, uf_stats, total_reclaimed, is_dry_run, job_id=None):
    """Send notification about cleanup results. Includes job log when available."""
    try:
        # Create Apprise instance using shared logic so SMTP env vars are honoured
        from app.notifications import get_apprise_instance, get_setting, get_subject_with_tag

        # Debug log to assist with dry-run notification troubleshooting
        print(f"[Cleanup] Sending cleanup notification ({'DRY RUN' if is_dry_run else 'LIVE'})")

        apobj = get_apprise_instance()
        if not apobj:
            print("[Cleanup] No apprise URLs or SMTP configured; skipping notification")
            return

        import apprise
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
    <li><strong>Old Logs:</strong> {log_stats.get('count', 0)} deleted</li>
    <li><strong>Unreferenced Directories:</strong> {temp_stats.get('count', 0)} removed ({format_bytes(temp_stats.get('reclaimed', 0))})</li>
    <li><strong>Total Reclaimed:</strong> {format_bytes(total_reclaimed)}</li>
</ul>
"""
        
        if is_dry_run:
            body += "\n<p><em>⚠️ This was a dry run - no files were actually deleted.</em></p>"
        
        body += f"""
<hr>
<p><small>Docker Archiver: <a href="{base_url}">{base_url}</a></small></p>"""
        
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

        # Get format preference from notifications module
        from app.notifications import get_notification_format, strip_html_tags
        body_format = get_notification_format()
        
        # Convert to plain text if needed
        if body_format == apprise.NotifyFormat.TEXT:
            body = strip_html_tags(body)
        
        apobj.notify(
            body=body,
            title=title,
            body_format=body_format
        )
        
    except Exception as e:
        print(f"[Cleanup] Failed to send notification: {e}")


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
        print('No report available. Use --run-cleanup to perform a cleanup (add --dry-run for a dry-run).')


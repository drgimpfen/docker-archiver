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


def run_cleanup(dry_run_override=None):
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
    
    # Create job record
    job_id = None
    log_lines = []
    
    def log_message(level, message):
        timestamp = utils.local_now().strftime('%Y-%m-%d %H:%M:%S')
        log_line = f"[{timestamp}] [{level}] {message}\n"
        log_lines.append(log_line)
        print(f"[Cleanup] {message}")
    
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
        temp_stats = cleanup_temp_files(is_dry_run, log_message)
        
        total_reclaimed = orphaned_stats.get('reclaimed', 0) + temp_stats.get('reclaimed', 0)
        
        log_message('INFO', f"Cleanup task completed ({mode})")
        log_message('INFO', f"Total reclaimed: {format_bytes(total_reclaimed)}")
        
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
            send_cleanup_notification(orphaned_stats, log_stats, temp_stats, total_reclaimed, is_dry_run)
            
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


def cleanup_temp_files(is_dry_run=False, log_callback=None):
    """Remove temporary files from failed jobs."""
    def log(message):
        if log_callback:
            log_callback('INFO', message)
        else:
            print(f"[Cleanup] {message}")
    
    log("Checking for temporary files...")
    
    archive_base = Path(ARCHIVE_BASE)
    if not archive_base.exists():
        return {'count': 0, 'reclaimed': 0}
    
    temp_count = 0
    reclaimed_bytes = 0
    
    # Find all .tmp files recursively
    for temp_file in archive_base.rglob('*.tmp'):
        size = temp_file.stat().st_size if temp_file.exists() else 0
        temp_count += 1
        reclaimed_bytes += size
        
        if is_dry_run:
            log(f"Would delete temp file: {temp_file.relative_to(archive_base)} ({format_bytes(size)})")
        else:
            log(f"Deleting temp file: {temp_file.relative_to(archive_base)} ({format_bytes(size)})")
            try:
                temp_file.unlink()
            except Exception as e:
                log(f"Failed to delete temp file {temp_file.relative_to(archive_base)}: {e}")
    
    # Find empty stack directories
    for archive_dir in archive_base.iterdir():
        if not archive_dir.is_dir() or archive_dir.name.startswith('_'):
            continue

        for stack_dir in archive_dir.iterdir():
            if not stack_dir.is_dir():
                continue

            # Check if stack directory is empty or has no valid backups
            if is_stack_directory_empty(stack_dir, log_callback=log):
                temp_count += 1

                if is_dry_run:
                    log(f"Would delete empty stack directory: {stack_dir.relative_to(archive_base)}")
                else:
                    log(f"Deleting empty stack directory: {stack_dir.relative_to(archive_base)}")
                    try:
                        shutil.rmtree(stack_dir)
                    except Exception as e:
                        log(f"Failed to delete empty stack directory {stack_dir.relative_to(archive_base)}: {e}")
    
    if temp_count > 0:
        log(f"Found {temp_count} temp file(s)/directory(ies), {format_bytes(reclaimed_bytes)} to reclaim")
    else:
        log("No temporary files found")
    
    return {'count': temp_count, 'reclaimed': reclaimed_bytes}


def is_stack_directory_empty(stack_dir, log_callback=None):
    """Check if a stack directory has no valid backups.

    Recognizes timestamped folders using the canonical compact format
    YYYYMMDD_HHMMSS (e.g. 20251221_174043).
    """
    if not any(stack_dir.iterdir()):
        return True  # Completely empty

    # Check for tar files
    has_tar = any(
        f.suffix in ['.tar', '.gz', '.zst']
        for f in stack_dir.iterdir()
        if f.is_file()
    )

    # Check for timestamp directories (folder mode).
    has_timestamp_dirs = any(
        d.is_dir() and is_valid_timestamp_dirname(d.name)
        for d in stack_dir.iterdir()
    )

    return not has_tar and not has_timestamp_dirs


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


def send_cleanup_notification(orphaned_stats, log_stats, temp_stats, total_reclaimed, is_dry_run):
    """Send notification about cleanup results."""
    try:
        import apprise
        from app.notifications import get_setting, get_subject_with_tag
        
        apprise_urls = get_setting('apprise_urls', '')
        if not apprise_urls:
            return
        
        apobj = apprise.Apprise()
        for url in apprise_urls.strip().split('\n'):
            url = url.strip()
            if url:
                apobj.add(url)
        
        if not apobj:
            return
        
        mode = "🧪 DRY RUN" if is_dry_run else "✅"
        
        # Build message
        title = get_subject_with_tag(f"{mode} Cleanup Task Completed")
        
        base_url = get_setting('base_url', 'http://localhost:8080')
        
        body = f"""
<h2>{mode} Cleanup Task</h2>

<h3>Summary</h3>
<ul>
    <li><strong>Orphaned Archives:</strong> {orphaned_stats.get('count', 0)} removed ({format_bytes(orphaned_stats.get('reclaimed', 0))})</li>
    <li><strong>Old Logs:</strong> {log_stats.get('count', 0)} deleted</li>
    <li><strong>Temp Files:</strong> {temp_stats.get('count', 0)} removed ({format_bytes(temp_stats.get('reclaimed', 0))})</li>
    <li><strong>Total Reclaimed:</strong> {format_bytes(total_reclaimed)}</li>
</ul>
"""
        
        if is_dry_run:
            body += "\n<p><em>⚠️ This was a dry run - no files were actually deleted.</em></p>"
        
        body += f"""
<hr>
<p><small>Docker Archiver: <a href="{base_url}">{base_url}</a></small></p>"""
        
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

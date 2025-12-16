import subprocess
import os
import sys
import shutil
from datetime import datetime
import psycopg2
from psycopg2.extras import DictCursor
try:
    import apprise
    import html as _html
except Exception:
    apprise = None

# --- HELPER FUNCTIONS ---

def get_db_connection():
    """Establishes a connection to the database using environment variables."""
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(database_url)

def format_bytes(size_bytes):
    """Converts bytes to human-readable format."""
    if size_bytes is None or size_bytes < 0:
        return "N/A"
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "K", "M", "G", "T")
    i = 0
    while size_bytes >= 1024 and i < len(size_name) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.1f}{size_name[i]}"

def log_to_db(job_id, message):
    """Appends a log message to a specific job entry in the database."""
    print(f"[Job {job_id}] {message}") # Also print to container logs
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE archive_jobs SET log = log || %s WHERE id = %s;",
            (f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n", job_id)
        )
        conn.commit()
    conn.close()


def _get_setting(key):
    """Retrieve a single value from the settings table."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s;", (key,))
            row = cur.fetchone()
            return row['value'] if row else None
    finally:
        conn.close()


def _set_setting(key, value):
    """Set or update a setting in the DB settings table."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = %s;", (key, value, value))
            conn.commit()
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def send_apprise_notification(title, body, job_id=None):
    """Send a notification via Apprise if configured. Safe to call when Apprise is not installed."""
    if apprise is None:
        if job_id:
            log_to_db(job_id, "Apprise library not available; skipping notification.")
        return

    enabled = _get_setting('apprise_enabled')
    if not enabled or str(enabled).lower() != 'true':
        return

    urls = _get_setting('apprise_urls') or ''
    url_list = [u.strip() for u in urls.splitlines() if u.strip()]
    if not url_list:
        if job_id:
            log_to_db(job_id, "No Apprise URLs configured; skipping notification.")
        return

    try:
        a = apprise.Apprise()
        for u in url_list:
            try:
                a.add(u)
            except Exception as e:
                if job_id:
                    log_to_db(job_id, f"Apprise: failed to add URL {u}: {e}")
        # For mailto URLs, ensure CRLF line endings for better mail client compatibility
        body_to_send = body or ''
        try:
            if any(u.lower().startswith('mailto') for u in url_list):
                body_to_send = body_to_send.replace('\n', '\r\n')
        except Exception:
            body_to_send = body_to_send

        # Build an HTML-safe <pre> version for HTML-capable notifiers
        try:
            html_body = '<pre style="white-space:pre-wrap;font-family:monospace;">' + _html.escape(body or '') + '</pre>'
        except Exception:
            html_body = '<pre>' + (body or '') + '</pre>'

        # Try sending with HTML body format if Apprise exposes a NotifyFormat enum
        sent = False
        try:
            if hasattr(apprise, 'common') and hasattr(apprise.common, 'NotifyFormat'):
                nf = apprise.common.NotifyFormat.HTML
                # Prefer sending the HTML-wrapped body when requesting HTML body format
                a.notify(title=title, body=html_body, body_format=nf)
                sent = True
        except Exception:
            sent = False

        # Fallback: append HTML <pre> block to plain body so services that render HTML will show it
        if not sent:
            try:
                a.notify(title=title, body=body_to_send + '\n\n' + html_body)
                sent = True
            except Exception:
                # Last resort: send plain text
                a.notify(title=title, body=body_to_send)
        if job_id:
            log_to_db(job_id, f"Notification sent: {title}")
    except Exception as e:
        if job_id:
            log_to_db(job_id, f"Apprise notification error: {e}")

def run_command(command, job_id, description):
    """Executes a shell command and logs the result to the DB."""
    try:
        log_to_db(job_id, f"Running: {description}...")
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=300)
        log_to_db(job_id, f"Success: {description}.")
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        error_message = f"Failed: {description}. Error:\n{e.stderr.strip()}"
        log_to_db(job_id, error_message)
        raise RuntimeError(error_message) # Propagate failure
    except subprocess.TimeoutExpired:
        error_message = f"Timeout: {description} took too long to execute."
        log_to_db(job_id, error_message)
        raise RuntimeError(error_message)
    except FileNotFoundError:
        error_message = "Error: A command (like 'docker' or 'tar') was not found."
        log_to_db(job_id, error_message)
        raise RuntimeError(error_message)

def compose_action(stack_path, job_id, action="down"):
    """Stops ('down') or starts ('up' -d) a Docker Compose stack."""
    if not os.path.isdir(stack_path):
        log_to_db(job_id, f"Stack directory not found at {stack_path}. Skipping {action}.")
        return
    
    action_text = "Stopping" if action == "down" else "Starting"
    compose_file = "compose.yaml" if os.path.exists(os.path.join(stack_path, "compose.yaml")) else "docker-compose.yml"
    compose_file_path = os.path.join(stack_path, compose_file)

    # Determine which compose command to use (cached detection)
    compose_cmd = None
    # Prefer docker-compose binary if present
    if shutil.which('docker-compose'):
        compose_cmd = ['docker-compose']
    else:
        # If `docker compose` (v2) is available, prefer it
        if shutil.which('docker'):
            try:
                # quick check whether 'docker compose version' runs
                subprocess.run(['docker', 'compose', 'version'], capture_output=True, check=True, timeout=5)
                compose_cmd = ['docker', 'compose']
            except Exception:
                compose_cmd = None

    if not compose_cmd:
        # Last resort: try docker-compose name anyway
        compose_cmd = ['docker-compose']

    cmd = compose_cmd + ['-f', compose_file_path, action]
    if action == 'up':
        cmd.append('-d')

    try:
        run_command(cmd, job_id, f"{action_text} stack in {stack_path}")
    except Exception as e:
        log_to_db(job_id, f"Compose command failed: {e}. Command tried: {cmd}")

def create_archive(stack_name, stack_path, backup_dir, job_id):
    """Creates a TAR archive and returns its path and size."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name_base = f"{stack_name}_{timestamp}.tar"
    final_backup_path = os.path.join(backup_dir, stack_name)
    os.makedirs(final_backup_path, exist_ok=True)
    
    target_filename = os.path.join(final_backup_path, archive_name_base)
    
    archive_root_dir = os.path.dirname(stack_path)
    dir_to_archive = os.path.basename(stack_path)

    cmd = ["tar", "-c", "-f", target_filename, "-C", archive_root_dir, dir_to_archive]
    
    run_command(cmd, job_id, f"Archiving {stack_name}")
    
    size_bytes = os.path.getsize(target_filename)
    log_to_db(job_id, f"Archive created: {target_filename}. Size: {format_bytes(size_bytes)}.")
    return target_filename, size_bytes

def cleanup_local_archives(archive_dir, retention_days_str, master_job_id):
    """Deletes old local archives based on retention days."""
    try:
        retention_days = int(retention_days_str)
    except (ValueError, TypeError):
        log_to_db(master_job_id, f"Invalid retention period '{retention_days_str}'. Skipping cleanup.")
        return

    log_to_db(master_job_id, f"Starting cleanup. Deleting archives older than {retention_days} days in {archive_dir}.")
    
    deleted_count = 0
    freed_space = 0

    for root, dirs, files in os.walk(archive_dir):
        for file in files:
            if file.endswith(".tar"):
                file_path = os.path.join(root, file)
                file_mtime = os.path.getmtime(file_path)
                if (datetime.now() - datetime.fromtimestamp(file_mtime)).days > retention_days:
                    try:
                        size = os.path.getsize(file_path)
                        os.remove(file_path)
                        log_to_db(master_job_id, f"Deleted old archive: {file_path}")
                        deleted_count += 1
                        freed_space += size
                    except OSError as e:
                        log_to_db(master_job_id, f"Error deleting file {file_path}: {e}")
    log_to_db(master_job_id, f"Cleanup finished. Deleted {deleted_count} files, freeing {format_bytes(freed_space)}.")
    # Return summary values for notifications
    return deleted_count, freed_space

# --- MAIN ORCHESTRATOR ---

def run_archive_job(selected_stack_paths, retention_days, archive_dir, master_name=None, master_description=None):
    """
    The main function to be run in a background thread.
    It orchestrates the entire archiving process for a list of stack paths.
    """
    # Create a "master" job entry for the overall process (mainly for cleanup logs)
    master_label = master_name or 'Master Run'
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(
            "INSERT INTO archive_jobs (stack_name, start_time, status, log) VALUES (%s, %s, %s, %s) RETURNING id;",
            (master_label, datetime.now(), 'Running', 'Starting archiving process...\n')
        )
        master_job_id = cur.fetchone()['id']
        conn.commit()
    conn.close()

    created_archives = []

    for stack_path in selected_stack_paths:
        stack_name = os.path.basename(stack_path)
        start_time = datetime.now()
        job_id = None
        
        # Create a specific job entry for this stack
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "INSERT INTO archive_jobs (stack_name, start_time, status, log) VALUES (%s, %s, %s, %s) RETURNING id;",
                (stack_name, start_time, 'Running', f"Starting archive for stack: {stack_name}\n")
            )
            job_id = cur.fetchone()['id']
            conn.commit()
        conn.close()

        try:
            # Verify the stack path exists before proceeding
            if not os.path.isdir(stack_path):
                raise FileNotFoundError(f"Stack directory not found at path: {stack_path}")

            # 1. Stop Stack
            compose_action(stack_path, job_id, action="down")
            
            # 2. Archive Stack
            archive_path, archive_size = create_archive(stack_name, stack_path, archive_dir, job_id)
            created_archives.append((archive_path, archive_size))
            
            # 3. Start Stack
            compose_action(stack_path, job_id, action="up")
            
            # 4. Update DB with Success
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE archive_jobs 
                    SET status = 'Success', end_time = %s, duration_seconds = %s, archive_path = %s, archive_size_bytes = %s, log = log || %s
                    WHERE id = %s;
                    """,
                    (end_time, int(duration), archive_path, archive_size, 'Archive completed successfully.', job_id)
                )
                conn.commit()
            conn.close()
            # send success notification for this stack
            try:
                send_apprise_notification(
                    title=f"Backup success: {stack_name} (Job {job_id})",
                    body=f"Job: {stack_name} (ID: {job_id})\nArchive created: {archive_path}\nSize: {format_bytes(archive_size)}\n\nLog:\n" + (get_job_log(job_id) or ''),
                    job_id=job_id
                )
            except Exception:
                pass

        except Exception as e:
            # Mark job as failed on any error
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            error_log = f"FATAL: Archive failed for {stack_name}. Reason: {e}\n"
            log_to_db(job_id, error_log)
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE archive_jobs SET status = 'Failed', end_time = %s, duration_seconds = %s WHERE id = %s;",
                    (end_time, int(duration), job_id)
                )
                conn.commit()
            conn.close()
            # Attempt to restart the stack even on failure
            try:
                log_to_db(job_id, "Attempting to restart the stack after failure...")
                compose_action(stack_path, job_id, action="up")
            except Exception as restart_e:
                log_to_db(job_id, f"Could not restart stack after failure: {restart_e}")
            # send failure notification for this stack
            try:
                send_apprise_notification(
                    title=f"Backup FAILED: {stack_name}",
                    body=f"Error: {e}",
                    job_id=job_id
                )
            except Exception:
                pass

    # After all stacks are processed, finish master job (cleanup is handled by dedicated cleanup job)
    try:
        log_to_db(master_job_id, 'Archiving process finished.')
        status = 'Success'
    except Exception as e:
        log_to_db(master_job_id, f'Archiving finish logging failed: {e}')
        status = 'Failed'

    # Mark master job as complete
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE archive_jobs SET status = %s, end_time = %s WHERE id = %s;", (status, datetime.now(), master_job_id))
        conn.commit()
    conn.close()
    # send master notification
    try:
        # Build summary block
        summary_lines = []
        summary_lines.append('SUMMARY OF CREATED ARCHIVES (Alphabetical by filename):')
        summary_lines.append('----------------------------------------------------------------')
        total_new = 0
        # sort by filename
        for path, size in sorted(created_archives, key=lambda x: os.path.basename(x[0]).lower()):
            summary_lines.append(f"{format_bytes(size):>10}    {path}")
            total_new += size
        summary_lines.append('----------------------------------------------------------------')
        summary_lines.append(f"{format_bytes(total_new):>10}    TOTAL SIZE OF NEW ARCHIVES")
        summary_lines.append('----------------------------------------------------------------')
        summary_lines.append('\n')
        # Disk usage
        try:
            usage = shutil.disk_usage('/')
            disk_line = f"Disk: / | Total: {usage.total//(1024**3)}G | Used: {usage.used//(1024**3)}G | Usage: {usage.used*100//usage.total}%"
        except Exception:
            disk_line = 'Disk usage: unavailable'
        summary_lines.append('DISK USAGE CHECK (on /):')
        summary_lines.append('----------------------------------------------------------------')
        summary_lines.append(disk_line)
        # backup dir size
        try:
            backup_size = get_dir_size(archive_dir)
            summary_lines.append(f"Backup Content Size ({archive_dir}): {format_bytes(backup_size)}")
        except Exception:
            summary_lines.append(f"Backup Content Size ({archive_dir}): unavailable")
        summary_lines.append('----------------------------------------------------------------')
        summary = '\n'.join(summary_lines)

        # fetch master log
        master_log = get_job_log(master_job_id) or ''

        # Include master description at top if provided
        desc_block = ''
        if master_description:
            desc_block = f"DESCRIPTION: {master_description}\n\n"

        body = desc_block + summary + '\n\nLOG:\n' + master_log

        job_name = get_job_name(master_job_id) or f"Job {master_job_id}"
        send_apprise_notification(
            title=f"Backup run {status} — {job_name}",
            body=f"Master job {job_name} (ID: {master_job_id}) completed with status: {status}\n\n" + body,
            job_id=master_job_id
        )
    except Exception:
        pass


def get_job_log(job_id):
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT log FROM archive_jobs WHERE id = %s;", (job_id,))
            row = cur.fetchone()
            return row['log'] if row and 'log' in row else ''
    except Exception:
        return ''
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_dir_size(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total


def get_job_name(job_id):
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT stack_name FROM archive_jobs WHERE id = %s;", (job_id,))
            row = cur.fetchone()
            return row['stack_name'] if row and 'stack_name' in row else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _delete_old_files_in_dir(target_dir, retention_days, master_job_id):
    """Delete .tar files in target_dir older than retention_days.
    Returns (deleted_count, freed_bytes, deleted_files_list).
    `deleted_files_list` is a list of tuples: (relative_path, size_bytes).
    """
    deleted_count = 0
    freed_space = 0
    deleted_files = []
    if not os.path.isdir(target_dir):
        log_to_db(master_job_id, f"Archive directory not found for cleanup: {target_dir}")
        return deleted_count, freed_space, deleted_files

    for fname in os.listdir(target_dir):
        if not fname.endswith('.tar'):
            continue
        fpath = os.path.join(target_dir, fname)
        try:
            mtime = os.path.getmtime(fpath)
        except Exception:
            continue
        try:
            if (datetime.now() - datetime.fromtimestamp(mtime)).days > int(retention_days):
                try:
                    size = os.path.getsize(fpath)
                except Exception:
                    size = 0
                try:
                    os.remove(fpath)
                    rel = os.path.relpath(fpath, start=target_dir)
                    log_to_db(master_job_id, f"Deleted old archive: {fpath} ({format_bytes(size)})")
                    deleted_count += 1
                    freed_space += size
                    deleted_files.append((fpath, size))
                except OSError as e:
                    log_to_db(master_job_id, f"Error deleting file {fpath}: {e}")
        except Exception:
            continue

    return deleted_count, freed_space, deleted_files


def run_cleanup_job(archive_dir, master_name=None, master_description=None, schedule_id=None):
    """Run cleanup across all configured schedules, applying each schedule's retention to its stacks.

    This creates a master job entry and logs per-stack cleanup actions. It sends a final summary notification.
    """
    master_label = master_name or 'Cleanup Job'
    # Acquire lock: prevent concurrent cleanup runs
    try:
        if str(_get_setting('cleanup_in_progress')).lower() == 'true':
            # log and exit
            # create a transient master job to record the refused attempt
            conn = get_db_connection()
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(
                    "INSERT INTO archive_jobs (stack_name, start_time, status, log) VALUES (%s, %s, %s, %s) RETURNING id;",
                    (master_label + ' (refused)', datetime.now(), 'Failed', 'Cleanup already in progress; refused to start.\n')
                )
                conn.commit()
            conn.close()
            return
        # mark in-progress
        _set_setting('cleanup_in_progress', 'true')
    except Exception:
        # if lock operations fail, proceed but log a warning (best-effort)
        pass
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(
            "INSERT INTO archive_jobs (stack_name, start_time, status, log) VALUES (%s, %s, %s, %s) RETURNING id;",
            (master_label, datetime.now(), 'Running', 'Starting cleanup across configured schedules...\n')
        )
        master_job_id = cur.fetchone()['id']
        conn.commit()
    conn.close()

    total_deleted = 0
    total_freed = 0
    per_schedule_summary = []

    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            if schedule_id:
                cur.execute("SELECT id, name, stack_paths, retention_days FROM schedules WHERE id = %s AND enabled = true;", (schedule_id,))
            else:
                cur.execute("SELECT id, name, stack_paths, retention_days FROM schedules WHERE enabled = true;")
            schedules = cur.fetchall()
        conn.close()
    except Exception as e:
        log_to_db(master_job_id, f"Failed to load schedules for cleanup: {e}")
        schedules = []

    for s in schedules:
        try:
            s_name = s.get('name') or f"schedule_{s.get('id')}"
            retention = s.get('retention_days') or 28
            paths = [p for p in (s.get('stack_paths') or '').split('\n') if p.strip()]
            schedule_deleted = 0
            schedule_freed = 0
            for sp in paths:
                stack_name = os.path.basename(sp)
                target_dir = os.path.join(archive_dir, stack_name)
                dcount, dfreed, dfiles = _delete_old_files_in_dir(target_dir, retention, master_job_id)
                schedule_deleted += dcount
                schedule_freed += dfreed
                # attach file details for this schedule
                if dfiles:
                    if 'files' not in locals():
                        files = {}
                    files.setdefault(s_name, []).extend(dfiles)
            total_deleted += schedule_deleted
            total_freed += schedule_freed
            per_schedule_summary.append((s_name, schedule_deleted, schedule_freed))
        except Exception as e:
            log_to_db(master_job_id, f"Cleanup error for schedule {s.get('name')}: {e}")

    # finalize master job
    log_to_db(master_job_id, f"Cleanup finished. Deleted {total_deleted} files, freeing {format_bytes(total_freed)}.")
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE archive_jobs SET status = %s, end_time = %s WHERE id = %s;", ('Success', datetime.now(), master_job_id))
        conn.commit()
    conn.close()

    # Build notification body
    try:
        desc_block = f"DESCRIPTION: {master_description}\n\n" if master_description else ''
        lines = [f"CLEANUP JOB: {master_label}", '----------------------------------------------------------------']
        # per-schedule summaries
        for sname, dcount, dfreed in per_schedule_summary:
            lines.append(f"{sname}: Deleted {dcount} files, Freed {format_bytes(dfreed)}")
            # include per-file listing if present
            if 'files' in locals() and files.get(sname):
                for fpath, fsize in files.get(sname):
                    lines.append(f"    - {fpath} ({format_bytes(fsize)})")
        lines.append('----------------------------------------------------------------')
        lines.append(f"TOTAL: Deleted {total_deleted} files, Freed {format_bytes(total_freed)}")
        body = desc_block + '\n'.join(lines)
        send_apprise_notification(title=f"Cleanup run completed — {master_label}", body=body, job_id=master_job_id)
    except Exception:
        pass
    finally:
        # release lock
        try:
            _set_setting('cleanup_in_progress', 'false')
        except Exception:
            pass

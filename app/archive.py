import subprocess
import os
import sys
import shutil
from datetime import datetime
import psycopg2
from psycopg2.extras import DictCursor
import retention
from db import get_db_connection
try:
    import apprise
    import html as _html
except Exception:
    apprise = None
    _html = None

# --- HELPER FUNCTIONS ---

# use centralized DB connection from app.db

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
    try:
        # Also mirror logs to unified jobs table if present
        with get_db_connection().cursor() as cur2:
            cur2.execute("UPDATE jobs SET log = COALESCE(log,'') || %s WHERE legacy_archive_id = %s;",
                         (f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n", job_id))
            cur2.connection.commit()
    except Exception:
        pass
    conn.close()


# retention logging moved to app/retention.py


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


def _detect_compose_cmd(stack_path):
    """Return the compose command list and compose file path for a stack."""
    compose_file = "compose.yaml" if os.path.exists(os.path.join(stack_path, "compose.yaml")) else "docker-compose.yml"
    compose_file_path = os.path.join(stack_path, compose_file)
    compose_cmd = None
    if shutil.which('docker-compose'):
        compose_cmd = ['docker-compose']
    else:
        if shutil.which('docker'):
            try:
                subprocess.run(['docker', 'compose', 'version'], capture_output=True, check=True, timeout=5)
                compose_cmd = ['docker', 'compose']
            except Exception:
                compose_cmd = None
    if not compose_cmd:
        compose_cmd = ['docker-compose']
    return compose_cmd, compose_file_path


def _is_compose_running(stack_path):
    """Return True if any container in the compose project is currently running."""
    try:
        compose_cmd, compose_file_path = _detect_compose_cmd(stack_path)
        # run `compose ps -q` to get container ids
        cmd = compose_cmd + ['-f', compose_file_path, 'ps', '-q']
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        out = (proc.stdout or '').strip()
        return bool(out)
    except Exception:
        return False


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

        # Build a nicer HTML body (header, preformatted body, footer links) when possible
        try:
            # Prefer DB-configured base URL (stored in settings), fall back to env var
            base_url = _get_setting('app_base_url') or os.environ.get('APP_BASE_URL', '')
            base_url = (base_url or '').rstrip('/')
        except Exception:
            base_url = ''

        try:
            logo_src = (base_url + '/static/assets/docker-archiver-logo.png') if base_url else '/static/assets/docker-archiver-logo.png'
        except Exception:
            logo_src = '/static/assets/docker-archiver-logo.png'

        try:
            safe_title = _html.escape(title or '')
            safe_body = _html.escape(body or '')
            timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
            links_html = ''
            if base_url:
                links_html = f'<a href="{base_url}/archives">Archives</a> | <a href="{base_url}/history">History</a>'
                if job_id:
                    links_html += f' | <a href="{base_url}/history#job-{job_id}">Job details</a>'

            # Use a slightly smaller logo and more compact header to fit the new logo
            html_body = f"""
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial; color:#222;">
  <div style="display:flex;align-items:center;border-bottom:1px solid #eee;padding:6px 0;margin-bottom:8px;gap:10px;">
    <img src="{logo_src}" alt="logo" style="height:28px;width:auto;border-radius:4px;object-fit:contain;"/>
    <div style="display:flex;flex-direction:column;">
      <div style="font-size:15px;font-weight:600;color:#111;line-height:1;">{safe_title}</div>
      <div style="font-size:12px;color:#666;line-height:1;">{timestamp}</div>
    </div>
  </div>
  <div style="padding:6px 0;white-space:pre-wrap;font-family:monospace;background:#f8f8f8;border-radius:6px;padding:12px;color:#111;">{safe_body}</div>
  <div style="border-top:1px solid #eee;padding-top:8px;margin-top:8px;font-size:13px;color:#666;">
    {links_html}
  </div>
</div>
"""
        except Exception:
            html_body = '<pre style="white-space:pre-wrap;font-family:monospace;">' + (body or '') + '</pre>'

        # Decide whether HTML notifications are enabled (default true)
        try:
            apprise_html_val = _get_setting('apprise_html')
            apprise_html_enabled = (str(apprise_html_val).lower() == 'true') if apprise_html_val is not None else True
        except Exception:
            apprise_html_enabled = True

        sent = False
        if apprise_html_enabled:
            try:
                if hasattr(apprise, 'common') and hasattr(apprise.common, 'NotifyFormat'):
                    nf = apprise.common.NotifyFormat.HTML
                    a.notify(title=title, body=html_body, body_format=nf)
                    sent = True
            except Exception:
                sent = False

            # Fallback: append HTML block to plain text body so services that render HTML will show it
            if not sent:
                try:
                    a.notify(title=title, body=body_to_send + '\n\n' + html_body)
                    sent = True
                except Exception:
                    try:
                        a.notify(title=title, body=body_to_send)
                        sent = True
                    except Exception:
                        sent = False
        else:
            # HTML disabled — send plain text only
            try:
                a.notify(title=title, body=body_to_send)
                sent = True
            except Exception:
                sent = False

        if job_id and sent:
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

def cleanup_local_archives(archive_dir, retention_days_str, job_id, job_table='archive'):
    """Wrapper that delegates to `retention.cleanup_local_archives` for retention logging.
    Keeps signature for backward compatibility.
    """
    if job_table == 'retention':
        return retention.cleanup_local_archives(archive_dir, retention_days_str, job_id, job_table='retention')
    # fallback: call retention but map logs to archive job id
    return retention.cleanup_local_archives(archive_dir, retention_days_str, job_id, job_table='retention')

# --- MAIN ORCHESTRATOR ---

def run_archive_job(selected_stack_paths, retention_days, archive_dir, archive_description=None, store_unpacked=False, job_type='manual'):
    """
    The main function to be run in a background thread.
    It orchestrates the entire archiving process for a list of stack paths.
    """
    # Create a top-level "archive" job entry for the overall process (mainly for cleanup logs)
    # No archive name is stored; use empty string to satisfy NOT NULL column until schema is migrated.
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(
            "INSERT INTO archive_jobs (stack_name, start_time, status, log, is_archive, job_type) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, job_id;",
            ('', datetime.now(), 'Running', 'Starting archiving process...\n', True, job_type)
        )
        res = cur.fetchone()
        archive_job_id = res['id']
        archive_job_seq = res.get('job_id')
        conn.commit()
    conn.close()

    # Mirror a top-level entry into unified `jobs` table (legacy mapping)
    try:
        connj = get_db_connection()
        with connj.cursor() as cj:
            cj.execute(
                "INSERT INTO jobs (legacy_archive_id, job_type, start_time, status, description, log) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id;",
                (archive_job_id, 'archive_master', datetime.now(), 'Running', archive_description, 'Starting archiving process...\n')
            )
            jres = cj.fetchone()
            jobs_master_id = jres[0] if jres else None
            connj.commit()
        connj.close()
    except Exception:
        jobs_master_id = None

    created_archives = []

    job_log = []  # Initialize job_log to avoid reference errors
    for stack_path in selected_stack_paths:
        stack_name = os.path.basename(stack_path)
        start_time = datetime.now()
        job_id = None
        
        # Create a specific job entry for this stack
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "INSERT INTO archive_jobs (stack_name, start_time, status, log, archive_id, job_type) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, job_id;",
                (stack_name, start_time, 'Running', f"Starting archive for stack: {stack_name}\n", archive_job_id, job_type)
            )
            res = cur.fetchone()
            job_id = res['id']
            job_seq = res.get('job_id')
            conn.commit()
        conn.close()
        # mirror per-stack entry into unified jobs table linking to master jobs entry
        try:
            connj = get_db_connection()
            with connj.cursor() as cj:
                cj.execute(
                    "INSERT INTO jobs (parent_id, legacy_archive_id, job_type, stack_name, start_time, status, log) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id;",
                    (jobs_master_id, job_id, 'archive_stack', stack_name, start_time, 'Running', f"Starting archive for stack: {stack_name}\n")
                )
                per_j = cj.fetchone()
                connj.commit()
            connj.close()
        except Exception:
            pass

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
            job_log.append(f"Archived {stack_name} -> {archive_path} ({format_bytes(archive_size)})")
            # add an entry to the master job log so the UI shows per-stack details
            try:
                log_to_db(archive_job_id, f"Archived {stack_name} -> {archive_path} ({format_bytes(archive_size)})")
            except Exception:
                pass
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE archive_jobs 
                    SET status = 'Success', end_time = %s, duration_seconds = %s, archive_path = %s, archive_size_bytes = %s, log = log || %s
                    WHERE id = %s;
                    """,
                    (end_time, max(1, int(duration)), archive_path, archive_size, f"Archive completed: {archive_path} ({format_bytes(archive_size)})", job_id)
                )
                conn.commit()
            conn.close()
            # mirror update to unified jobs table if present
            try:
                cj = get_db_connection()
                with cj.cursor() as c2:
                    c2.execute(
                        "UPDATE jobs SET status=%s, end_time=%s, duration_seconds=%s, archive_path=%s, archive_size_bytes=%s, log = COALESCE(log,'') || %s WHERE legacy_archive_id = %s;",
                        ('Success', end_time, max(1, int(duration)), archive_path, archive_size, f"Archive completed: {archive_path} ({format_bytes(archive_size)})\n", job_id)
                    )
                    cj.commit()
                cj.close()
            except Exception:
                pass
            # 5. Optionally create unpacked snapshot directory and update `latest` pointer when requested
            if store_unpacked:
                try:
                    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
                    snapshot_dir = os.path.join(archive_dir, stack_name, ts)
                    os.makedirs(snapshot_dir, exist_ok=True)
                    # Extract the tar into the snapshot directory (use run_command to log)
                    try:
                        run_command(['tar', 'xf', archive_path, '-C', snapshot_dir], job_id, f"Extracting {archive_path} to {snapshot_dir}")
                        log_to_db(job_id, f"Created unpacked snapshot: {snapshot_dir}")
                    except Exception as e:
                        log_to_db(job_id, f"Failed to extract archive to snapshot dir: {e}")

                    # Create or update a 'latest' symlink (or fallback to a text pointer on platforms without symlink support)
                    latest_link = os.path.join(archive_dir, stack_name, 'latest')
                    try:
                        if os.path.islink(latest_link) or os.path.exists(latest_link):
                            try:
                                os.remove(latest_link)
                            except Exception:
                                # ignore removal failures
                                pass
                        os.symlink(snapshot_dir, latest_link)
                    except Exception:
                        # Fallback: write a small pointer file with the path
                        try:
                            with open(latest_link + '.txt', 'w', encoding='utf-8') as pf:
                                pf.write(snapshot_dir)
                        except Exception:
                            log_to_db(job_id, f"Warning: could not create latest pointer for {snapshot_dir}")
                except Exception as e:
                    log_to_db(job_id, f"Warning: failed to create unpacked snapshot for {stack_name}: {e}")
            # per-stack notifications suppressed; master notification will summarize all stacks

        except Exception as e:
            # Mark job as failed on any error
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            error_log = f"FATAL: Archive failed for {stack_name}. Reason: {e}\n"
            log_to_db(job_id, error_log)
            try:
                log_to_db(archive_job_id, f"FAILED: {stack_name} -> {e}")
            except Exception:
                pass
            conn = get_db_connection()
                # Log the successful archiving
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE archive_jobs SET status = 'Failed', end_time = %s, duration_seconds = %s WHERE id = %s;",
                    (end_time, int(duration), job_id)
                )
                conn.commit()
            conn.close()
            try:
                cj = get_db_connection()
                with cj.cursor() as c2:
                    c2.execute("UPDATE jobs SET status=%s, end_time=%s, duration_seconds=%s WHERE legacy_archive_id = %s;",
                               ('Failed', end_time, int(duration), job_id))
                    cj.commit()
                cj.close()
            except Exception:
                pass
            # Attempt to restart the stack even on failure
            try:
                log_to_db(job_id, "Attempting to restart the stack after failure...")
                compose_action(stack_path, job_id, action="up")
            except Exception as restart_e:
                log_to_db(job_id, f"Could not restart stack after failure: {restart_e}")
            # per-stack failure notifications suppressed; master notification will report failures

    # After all stacks are processed, finish archive job (cleanup is handled by dedicated cleanup job)
    try:
        log_to_db(archive_job_id, 'Archiving process finished.')
        status = 'Success'
    except Exception as e:
        log_to_db(archive_job_id, f'Archiving finish logging failed: {e}')
        status = 'Failed'

    # Perform retention cleanup automatically after archiving by delegating to the retention module
    try:
        cleanup_summary_lines = retention.run_retention_for_archives(created_archives, archive_dir, archive_job_id, archive_description, jobs_master_id if 'jobs_master_id' in locals() else None)
    except Exception:
        cleanup_summary_lines = []

    # Mark archive job as complete
    conn = get_db_connection()
    try:
        # update legacy archive_jobs with end_time
        end_time = datetime.now()
        with conn.cursor() as cur:
            cur.execute("UPDATE archive_jobs SET status = %s, end_time = %s WHERE id = %s;", (status, end_time, archive_job_id))
            conn.commit()
        # compute total size of created archives
        total_new = sum([s for (_, s) in created_archives]) if created_archives else 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Mirror to unified jobs table: include duration and total archive size when possible
    try:
        # attempt to read start_time from legacy archive_jobs to compute duration
        start_time = None
        cj = get_db_connection()
        with cj.cursor(cursor_factory=DictCursor) as csel:
            csel.execute("SELECT start_time FROM archive_jobs WHERE id = %s;", (archive_job_id,))
            row = csel.fetchone()
            if row and 'start_time' in row:
                start_time = row['start_time']
        if start_time:
            duration_seconds = max(1, int((end_time - start_time).total_seconds()))
        else:
            duration_seconds = None

        with cj.cursor() as c2:
            # update jobs row(s) that mirror this archive master
            if duration_seconds is not None:
                c2.execute(
                    "UPDATE jobs SET status=%s, end_time=%s, duration_seconds=%s, archive_size_bytes=%s, log = COALESCE(log,'') || %s WHERE legacy_archive_id = %s;",
                    (status, end_time, duration_seconds, total_new, f"Master finished. Total new archives size: {format_bytes(total_new)}\n", archive_job_id)
                )
            else:
                c2.execute(
                    "UPDATE jobs SET status=%s, end_time=%s, archive_size_bytes=%s, log = COALESCE(log,'') || %s WHERE legacy_archive_id = %s;",
                    (status, end_time, total_new, f"Master finished. Total new archives size: {format_bytes(total_new)}\n", archive_job_id)
                )
            cj.commit()
        cj.close()
    except Exception:
        pass

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
        # archive dir size
        try:
            backup_size = get_dir_size(archive_dir)
            summary_lines.append(f"Archive Content Size ({archive_dir}): {format_bytes(backup_size)}")
        except Exception:
            summary_lines.append(f"Archive Content Size ({archive_dir}): unavailable")
        summary_lines.append('----------------------------------------------------------------')
        summary = '\n'.join(summary_lines)

        # fetch archive log
        archive_log = get_job_log(archive_job_id) or ''

        # Include archive description at top if provided
        desc_block = ''
        if archive_description:
            desc_block = f"DESCRIPTION: {archive_description}\n\n"

        # If automatic retention ran and produced summary lines, include them here
        if 'cleanup_summary_lines' in locals() and cleanup_summary_lines:
            retention_block = ['\n', 'RETENTION SUMMARY:', '----------------------------------------------------------------']
            retention_block.extend(cleanup_summary_lines)
            retention_block.append('----------------------------------------------------------------')
            summary = summary + '\n' + '\n'.join(retention_block)

        body = desc_block + summary + '\n\nLOG:\n' + archive_log

        job_name = get_job_name(archive_job_id) or f"Job {archive_job_id}"
        send_apprise_notification(
            title=f"Archive run {status} — {job_name}",
            body=f"Archive job {job_name} (ID: {archive_job_id}) completed with status: {status}\n\n" + body,
            job_id=archive_job_id
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
# Retention/cleanup logic moved to app/retention.py

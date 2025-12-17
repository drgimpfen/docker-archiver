import os
import shutil
import subprocess
from datetime import datetime
import psycopg2
from psycopg2.extras import DictCursor
from db import get_db_connection
try:
    import apprise
    import html as _html
except Exception:
    apprise = None


# centralized DB connection provided by app.db


def format_bytes(size_bytes):
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


# send Apprise notification (copied minimal implementation)
def send_apprise_notification(title, body, job_id=None):
    if apprise is None:
        return
    enabled = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s;", ('apprise_enabled',))
            row = cur.fetchone()
            enabled = row[0] if row else None
        conn.close()
    except Exception:
        enabled = None
    if not enabled or str(enabled).lower() != 'true':
        return
    urls = ''
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s;", ('apprise_urls',))
            row = cur.fetchone()
            urls = row[0] if row else ''
        conn.close()
    except Exception:
        urls = ''
    url_list = [u.strip() for u in (urls or '').splitlines() if u.strip()]
    if not url_list:
        return
    try:
        a = apprise.Apprise()
        for u in url_list:
            try:
                a.add(u)
            except Exception:
                pass
        # send plain text body
        try:
            a.notify(title=title, body=body)
            # Note: logging to DB is handled by caller if desired
        except Exception:
            pass
    except Exception:
        pass


def log_retention_to_db(job_id, message):
    print(f"[Retention {job_id}] {message}")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE retention_jobs SET log = COALESCE(log, '') || %s WHERE id = %s;",
                (f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n", job_id)
            )
            conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # mirror into unified jobs table when available via jobs helper
    try:
        import jobs
        jobs.append_log_by_legacy_retention(job_id, message)
    except Exception:
        pass


def cleanup_local_archives(archive_dir, retention_days_str, job_id, job_table='retention'):
    try:
        retention_days = int(retention_days_str)
    except (ValueError, TypeError):
        log_retention_to_db(job_id, f"Invalid retention period '{retention_days_str}'. Skipping cleanup.")
        return 0, 0

    log_retention_to_db(job_id, f"Starting cleanup. Deleting archives older than {retention_days} days in {archive_dir}.")

    deleted_count = 0
    freed_space = 0

    for root, dirs, files in os.walk(archive_dir):
        for file in files:
            if file.endswith('.tar'):
                file_path = os.path.join(root, file)
                try:
                    file_mtime = os.path.getmtime(file_path)
                except Exception:
                    continue
                if (datetime.now() - datetime.fromtimestamp(file_mtime)).days > retention_days:
                    try:
                        size = os.path.getsize(file_path)
                        os.remove(file_path)
                        log_retention_to_db(job_id, f"Deleted old archive: {file_path} ({format_bytes(size)})")
                        deleted_count += 1
                        freed_space += size
                    except OSError as e:
                        log_retention_to_db(job_id, f"Error deleting file {file_path}: {e}")
    log_retention_to_db(job_id, f"Cleanup finished. Deleted {deleted_count} files, freeing {format_bytes(freed_space)}.")
    return deleted_count, freed_space


def run_retention_now(archive_dir, archive_description=None, job_type='manual'):
    # simple manual retention across all stacks
    try:
        # Prevent double runs
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s;", ('cleanup_in_progress',))
            row = cur.fetchone()
            if row and str(row[0]).lower() == 'true':
                # write a failing retention record
                cur.execute("INSERT INTO retention_jobs (start_time, status, log, job_type, description) VALUES (%s,%s,%s,%s,%s) RETURNING id;",
                            (datetime.now(), 'Failed', 'Retention already in progress; refused to start.\n', job_type, archive_description))
                conn.commit()
                conn.close()
                return
        conn.close()
    except Exception:
        pass

    try:
        start_time = datetime.now()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO retention_jobs (start_time, status, log, job_type, description) VALUES (%s,%s,%s,%s,%s) RETURNING id;",
                        (start_time, 'Running', 'Starting retention run across archive folders...\n', job_type, archive_description))
            res = cur.fetchone()
            retention_job_id = res[0]
            conn.commit()
        conn.close()
        # mirror into unified jobs table
        try:
            cj = get_db_connection()
            with cj.cursor() as c2:
                # mirror into unified jobs table
                import jobs
                jobs.create_job_for_retention(retention_job_id, None, 'retention', datetime.now(), 'Running', archive_description, 'Starting retention run across archive folders...\n')
                cj.commit()
            cj.close()
        except Exception:
            pass
    except Exception as e:
        return

    total_deleted = 0
    total_freed = 0
    per_stack = []

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s;", ('retention_days',))
            row = cur.fetchone()
            retention_val = row[0] if row else None
        conn.close()
    except Exception:
        retention_val = None
    try:
        retention_int = int(retention_val) if retention_val is not None else 28
    except Exception:
        retention_int = 28

    try:
        for entry in os.listdir(archive_dir):
            stack_dir = os.path.join(archive_dir, entry)
            if not os.path.isdir(stack_dir):
                continue
            try:
                dcount, dfreed = cleanup_local_archives(stack_dir, retention_int, retention_job_id, job_table='retention')
                per_stack.append((entry, dcount, dfreed))
                total_deleted += dcount
                total_freed += dfreed
            except Exception as e:
                log_retention_to_db(retention_job_id, f"Retention error for {entry}: {e}")
    except Exception as e:
        log_retention_to_db(retention_job_id, f"Failed to list archive dir for retention: {e}")

    end_time = datetime.now()
    # Compute duration from the recorded start time
    try:
        duration = int((end_time - start_time).total_seconds())
    except Exception:
        duration = 0
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE retention_jobs SET status=%s, end_time=%s, duration_seconds=%s, reclaimed_bytes=%s WHERE id=%s;",
                        ('Success', end_time, duration, total_freed, retention_job_id))
            conn.commit()
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

    # notify
    try:
        lines = [f"RETENTION RUN", '----------------------------------------------------------------']
        for name, dcount, dfreed in per_stack:
            lines.append(f"{name}: Deleted {dcount} files, Freed {format_bytes(dfreed)}")
        lines.append('----------------------------------------------------------------')
        lines.append(f"TOTAL: Deleted {total_deleted} files, Freed {format_bytes(total_freed)}")
        body = (f"DESCRIPTION: {archive_description}\n\n" if archive_description else '') + '\n'.join(lines)
        send_apprise_notification(title="Retention run completed", body=body, job_id=retention_job_id)
    except Exception:
        pass


def run_retention_for_archives(created_archives, archive_dir, archive_job_id, archive_description=None, parent_jobs_id=None):
    # created_archives: list of tuples (archive_path, size)
    # create a retention_jobs row linked to the archive_job
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            r_start = datetime.now()
            cur.execute("INSERT INTO retention_jobs (archive_job_id, start_time, status, log, job_type, description) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id;",
                        (archive_job_id, r_start, 'Running', 'Automatic retention started by archive run\n', 'automatic', archive_description))
            res = cur.fetchone()
            retention_job_id = res[0]
            conn.commit()
        conn.close()
    except Exception:
        return

    # Mirror retention job to unified jobs table, linking to parent jobs id when available
    try:
        cj = get_db_connection()
        with cj.cursor() as c2:
            # Mirror retention job to unified jobs table, linking to parent jobs id when available
            import jobs
            jobs.create_job_for_retention(retention_job_id, parent_jobs_id, 'retention', r_start, 'Running', archive_description, 'Automatic retention started by archive run\n')
            cj.commit()
        cj.close()
    except Exception:
        pass

    # determine stacks from created_archives
    stack_names = set()
    for path, _ in created_archives:
        try:
            stack_names.add(os.path.basename(os.path.dirname(path)))
        except Exception:
            continue

    total_deleted = 0
    total_freed = 0
    cleanup_summary_lines = []

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s;", ('retention_days',))
            row = cur.fetchone()
            retention_val = row[0] if row else None
        conn.close()
    except Exception:
        retention_val = None
    try:
        retention_int = int(retention_val) if retention_val is not None else 28
    except Exception:
        retention_int = 28

    for stack in sorted(stack_names):
        target_dir = os.path.join(archive_dir, stack)
        if not os.path.isdir(target_dir):
            cleanup_summary_lines.append(f"{stack}: archive dir missing")
            continue
        try:
            dcount, freed = cleanup_local_archives(target_dir, retention_int, retention_job_id, job_table='retention')
            cleanup_summary_lines.append(f"{stack}: Deleted {dcount} files, Freed {format_bytes(freed)}")
            total_deleted += dcount
            total_freed += freed
        except Exception as e:
            log_retention_to_db(retention_job_id, f"Retention error for {stack}: {e}")
            cleanup_summary_lines.append(f"{stack}: error during retention")

    # finalize
    try:
        r_end = datetime.now()
        r_dur = int((r_end - r_start).total_seconds())
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE retention_jobs SET status=%s, end_time=%s, duration_seconds=%s, reclaimed_bytes=%s, log=COALESCE(log,'') || %s WHERE id=%s;",
                        ('Success', r_end, r_dur, total_freed, '\n'.join(cleanup_summary_lines), retention_job_id))
            conn.commit()
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

    # include retention summary for master archive notification
    return cleanup_summary_lines

"""
Scheduler for automatic archive jobs.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.db import get_db
from app.executor import ArchiveExecutor
from app.notifications import get_setting
import os
import time
import threading


scheduler = None


def init_scheduler():
    """Initialize and start the scheduler."""
    global scheduler
    
    if scheduler is not None:
        return scheduler

    # Use a container-local sentinel to ensure only one process initializes the scheduler
    sentinel = '/tmp/da_scheduler_started'
    created = False
    try:
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        created = True
    except FileExistsError:
        created = False
    except Exception:
        # If any filesystem error occurs, proceed cautiously and attempt to start scheduler
        created = True

    if not created:
        print("[Scheduler] Initialization skipped (scheduler already started by another process)")
        return None

    from app.utils import get_display_timezone
    display_tz = get_display_timezone()

    # Create scheduler with explicit display timezone so cron triggers behave consistently
    scheduler = BackgroundScheduler(timezone=display_tz, daemon=True)
    scheduler.start()

    # Load all scheduled archives (with error handling for initial setup)
    try:
        reload_schedules()
    except Exception as e:
        print(f"[Scheduler] Could not load schedules during init (database might not be ready yet): {e}")

    # Add cleanup job for expired download tokens (runs daily)
    from app.downloads import cleanup_expired_tokens
    scheduler.add_job(
        cleanup_expired_tokens,
        'cron',
        hour=2,
        minute=0,
        id='cleanup_tokens',
        replace_existing=True
    )

    # Schedule main cleanup task
    schedule_cleanup_task()

    # Start Redis subscriber for hot-reloads if configured
    try:
        start_redis_listener()
    except Exception as e:
        print(f"[Scheduler] Redis listener failed to start: {e}")

    print("[Scheduler] Initialized and started")
    return scheduler


def schedule_cleanup_task():
    """Schedule or reschedule the cleanup task based on settings."""
    global scheduler

    if scheduler is None:
        return

    enabled = get_setting('cleanup_enabled', 'true').lower() == 'true'

    if not enabled:
        # Remove job if it exists
        if scheduler.get_job('cleanup_task'):
            scheduler.remove_job('cleanup_task')
            print("[Scheduler] Scheduled cleanup task disabled")
        return

    # Get cleanup cron from settings (format: minute hour day month day_of_week)
    cleanup_cron = get_setting('cleanup_cron', '30 2 * * *')
    cron_parts = cleanup_cron.split()
    if len(cron_parts) != 5:
        print(f"[Scheduler] Invalid cleanup_cron '{cleanup_cron}', using default '30 2 * * *'")
        cron_parts = ['30', '2', '*', '*', '*']

    from app.cleanup import run_cleanup

    try:
        from app.utils import get_display_timezone
        display_tz = get_display_timezone()
        trigger = CronTrigger(
            minute=cron_parts[0],
            hour=cron_parts[1],
            day=cron_parts[2],
            month=cron_parts[3],
            day_of_week=cron_parts[4],
            timezone=display_tz
        )
        scheduler.add_job(
            run_cleanup,
            trigger,
            id='cleanup_task',
            replace_existing=True
        )
        print(f"[Scheduler] Scheduled cleanup task with cron: {cleanup_cron}")
    except Exception as e:
        print(f"[Scheduler] Failed to schedule cleanup task: {e}")


# --------------------- Redis-based hot reload helpers ---------------------

def _redis_client():
    """Return a redis client built from REDIS_URL or None if not configured or unavailable."""
    url = os.environ.get('REDIS_URL')
    if not url:
        return None
    try:
        import redis
        return redis.from_url(url)
    except Exception as e:
        print(f"[Scheduler] Redis client unavailable: {e}")
        return None


def publish_reload_signal():
    """Publish a reload message on the scheduler channel to notify other processes."""
    client = _redis_client()
    if not client:
        return False
    try:
        client.publish('scheduler:reload', 'reload')
        return True
    except Exception as e:
        print(f"[Scheduler] Failed to publish reload signal: {e}")
        return False


def start_redis_listener():
    """Start a background thread that subscribes to Redis and calls reload_schedules() on messages."""
    client = _redis_client()
    if not client:
        print("[Scheduler] REDIS_URL not set — redis-based scheduler reload disabled")
        return

    def _run():
        while True:
            try:
                pub = client.pubsub(ignore_subscribe_messages=True)
                pub.subscribe('scheduler:reload')
                print("[Scheduler] Subscribed to Redis channel 'scheduler:reload' for hot-reloads")
                for message in pub.listen():
                    if not message:
                        continue
                    try:
                        print("[Scheduler] Received reload signal from Redis, reloading schedules")
                        reload_schedules()
                    except Exception as e:
                        print(f"[Scheduler] Error on reload from Redis: {e}")
                # If listen loop exits try to reconnect
            except Exception as e:
                print(f"[Scheduler] Redis listener error: {e}, retrying in 5s")
                time.sleep(5)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def reload_schedules():
    """Reload all archive schedules from database."""
    global scheduler
    
    if scheduler is None:
        return
    
    # Check maintenance mode
    maintenance_mode = get_setting('maintenance_mode', 'false').lower() == 'true'
    
    # Remove all archive jobs
    for job in scheduler.get_jobs():
        if job.id.startswith('archive_'):
            scheduler.remove_job(job.id)
    
    if maintenance_mode:
        print("[Scheduler] Maintenance mode enabled - no schedules loaded")
        return
    
    # Load enabled archive schedules
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM archives 
            WHERE schedule_enabled = true 
            AND schedule_cron IS NOT NULL
            AND schedule_cron != '';
        """)
        archives = cur.fetchall()
    
    display_tz = None
    try:
        from app.utils import get_display_timezone
        display_tz = get_display_timezone()
    except Exception:
        display_tz = None

    for archive in archives:
        try:
            # Parse cron expression
            # Format: minute hour day month day_of_week
            cron_parts = archive['schedule_cron'].split()
            if len(cron_parts) != 5:
                print(f"[Scheduler] Invalid cron expression for archive {archive['name']}: {archive['schedule_cron']}")
                continue
            
            # Use the configured display timezone for triggers so scheduled times match UI
            trigger_kwargs = dict(
                minute=cron_parts[0],
                hour=cron_parts[1],
                day=cron_parts[2],
                month=cron_parts[3],
                day_of_week=cron_parts[4]
            )
            if display_tz is not None:
                trigger_kwargs['timezone'] = display_tz

            trigger = CronTrigger(**trigger_kwargs)
            
            # Allow a short misfire grace so jobs that just missed due to timing/worker switchover
            # are executed if within the grace window (seconds)
            scheduler.add_job(
                run_scheduled_archive,
                trigger,
                args=[dict(archive)],
                id=f"archive_{archive['id']}",
                name=f"Archive: {archive['name']}",
                replace_existing=True,
                misfire_grace_time=300
            )
            
            print(f"[Scheduler] Scheduled archive '{archive['name']}' with cron: {archive['schedule_cron']}")
            
        except Exception as e:
            print(f"[Scheduler] Failed to schedule archive {archive['name']}: {e}")
    
    print(f"[Scheduler] Loaded {len(archives)} scheduled archive(s)")


def run_scheduled_archive(archive_config):
    """Run an archive job (called by scheduler)."""
    print(f"[Scheduler] Starting scheduled archive: {archive_config['name']}")
    
    try:
        # Start the scheduled archive as a detached subprocess to avoid blocking the scheduler
        import subprocess, sys, os
        from app import utils as _utils
        jobs_dir = os.environ.get('ARCHIVE_JOB_LOG_DIR', '/var/log/archiver')
        os.makedirs(jobs_dir, exist_ok=True)
        timestamp = _utils.local_now().strftime('%Y%m%d_%H%M%S')
        safe_name = _utils.filename_safe(archive_config['name'])
        log_name = f"{timestamp}_scheduled_{safe_name}.log"
        log_path = os.path.join(jobs_dir, log_name)
        cmd = [sys.executable, '-m', 'app.run_job', '--archive-id', str(archive_config['id']), '--log-path', log_path]
        subprocess.Popen(cmd, start_new_session=True)
        print(f"[Scheduler] Enqueued scheduled archive {archive_config['name']} (log: {log_name})")
    except Exception as e:
        print(f"[Scheduler] Archive failed: {e}")
        from app.notifications import send_error_notification
        send_error_notification(archive_config['name'], str(e))

def get_next_run_time(archive_id):
    """Get next run time for a scheduled archive.

    Two strategies are used:
    1. If the in-process scheduler exists, query it for the job's next_run_time.
    2. If no scheduler is present (common in multi-process deployments), compute the next
       run time from the archive's cron expression stored in the database using
       apscheduler.CronTrigger.
    """
    global scheduler

    # Strategy 1: If we have a live scheduler object, prefer it (accurate and uses the
    # scheduler's timezone settings).
    if scheduler is not None:
        job = scheduler.get_job(f"archive_{archive_id}")
        if job and job.next_run_time:
            try:
                # Keep timezone-aware UTC datetime for correct formatting and JS parsing
                from datetime import timezone
                next_run = job.next_run_time.astimezone(timezone.utc)
            except Exception:
                nr = job.next_run_time
                try:
                    # If job.next_run_time is naive, assume it's UTC
                    from datetime import timezone
                    next_run = nr.replace(tzinfo=timezone.utc)
                except Exception:
                    next_run = nr
            try:
                print(f"[Scheduler] Next run for archive_{archive_id}: {next_run.isoformat()}")
            except Exception:
                print(f"[Scheduler] Next run for archive_{archive_id}: {next_run}")
            return next_run

    # Strategy 2: Fallback to DB-backed computation when scheduler is not available in
    # this process. This covers deployments where the scheduler runs in a different
    # process/container and we still want the dashboard to show the next run.
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT schedule_cron FROM archives WHERE id = %s", (archive_id,))
            row = cur.fetchone()
            cron_expr = row['schedule_cron'] if row else None
    except Exception as e:
        print(f"[Scheduler] Could not read schedule from DB for archive_{archive_id}: {e}")
        cron_expr = None

    if not cron_expr:
        return None

    # Parse cron expression (minute hour day month day_of_week)
    cron_parts = cron_expr.split()
    if len(cron_parts) != 5:
        print(f"[Scheduler] Invalid cron expression for archive_{archive_id}: {cron_expr}")
        return None

    try:
        from datetime import datetime, timezone as _tz
        from app.utils import get_display_timezone
        display_tz = get_display_timezone()

        # Interpret the cron expression in the configured display timezone so that
        # cron times map to local clock times (e.g., '0 3 * * *' means 03:00 local).
        trigger = CronTrigger(
            minute=cron_parts[0],
            hour=cron_parts[1],
            day=cron_parts[2],
            month=cron_parts[3],
            day_of_week=cron_parts[4],
            timezone=display_tz
        )
        # Ask the trigger for the next fire time relative to now in the display tz
        now = datetime.now(display_tz)
        next_run = trigger.get_next_fire_time(None, now)
        if next_run:
            # Keep timezone-aware UTC datetime for correct formatting/parsing
            next_run_utc = next_run.astimezone(_tz.utc)
            try:
                print(f"[Scheduler] Next run (computed) for archive_{archive_id}: {next_run_utc.isoformat()} (local: {next_run.isoformat()})")
            except Exception:
                print(f"[Scheduler] Next run (computed) for archive_{archive_id}: {next_run_utc} (local: {next_run})")
            return next_run_utc
    except Exception as e:
        print(f"[Scheduler] Could not compute next run from cron for archive_{archive_id}: {e}")

    return None


def get_prev_run_time(archive_id):
    """Get previous scheduled run time for an archive (UTC-naive datetime) if computable."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT schedule_cron FROM archives WHERE id = %s", (archive_id,))
            row = cur.fetchone()
            cron_expr = row['schedule_cron'] if row else None
    except Exception as e:
        print(f"[Scheduler] Could not read schedule from DB for archive_{archive_id}: {e}")
        cron_expr = None

    if not cron_expr:
        return None

    try:
        from datetime import datetime, timezone as _tz
        from croniter import croniter
        from app.utils import get_display_timezone

        display_tz = get_display_timezone()
        now = datetime.now(display_tz)

        ci = croniter(cron_expr, now)
        prev_local = ci.get_prev(datetime)
        if prev_local:
            # Convert to timezone-aware UTC datetime for consistent handling
            prev_utc = prev_local.astimezone(_tz.utc)
            try:
                print(f"[Scheduler] Prev run (computed) for archive_{archive_id}: {prev_utc.isoformat()} (local: {prev_local.isoformat()})")
            except Exception:
                print(f"[Scheduler] Prev run (computed) for archive_{archive_id}: {prev_utc} (local: {prev_local})")
            return prev_utc
    except Exception as e:
        print(f"[Scheduler] Could not compute previous run from cron for archive_{archive_id}: {e}")

    return None

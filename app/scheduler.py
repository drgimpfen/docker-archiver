"""
Scheduler for automatic archive jobs.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.db import get_db
from app.executor import ArchiveExecutor
from app.notifications import get_setting
import os


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

    scheduler = BackgroundScheduler(daemon=True)
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
            print("[Scheduler] Cleanup task disabled")
        return
    
    # Get cleanup time from settings (format: HH:MM)
    cleanup_time = get_setting('cleanup_time', '02:30')
    try:
        hour, minute = map(int, cleanup_time.split(':'))
    except (ValueError, AttributeError):
        hour, minute = 2, 30  # Default fallback
        print(f"[Scheduler] Invalid cleanup_time format '{cleanup_time}', using default 02:30")
    
    from app.cleanup import run_cleanup
    
    scheduler.add_job(
        run_cleanup,
        'cron',
        hour=hour,
        minute=minute,
        id='cleanup_task',
        replace_existing=True
    )
    
    print(f"[Scheduler] Cleanup task scheduled for {hour:02d}:{minute:02d}")


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
    
    for archive in archives:
        try:
            # Parse cron expression
            # Format: minute hour day month day_of_week
            cron_parts = archive['schedule_cron'].split()
            if len(cron_parts) != 5:
                print(f"[Scheduler] Invalid cron expression for archive {archive['name']}: {archive['schedule_cron']}")
                continue
            
            trigger = CronTrigger(
                minute=cron_parts[0],
                hour=cron_parts[1],
                day=cron_parts[2],
                month=cron_parts[3],
                day_of_week=cron_parts[4]
            )
            
            scheduler.add_job(
                run_scheduled_archive,
                trigger,
                args=[dict(archive)],
                id=f"archive_{archive['id']}",
                name=f"Archive: {archive['name']}",
                replace_existing=True
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
        jobs_dir = os.environ.get('ARCHIVE_JOB_LOG_DIR', '/var/log/archiver')
        os.makedirs(jobs_dir, exist_ok=True)
        log_path = os.path.join(jobs_dir, f"archive_sched_{archive_config['id']}.log")
        cmd = [sys.executable, '-m', 'app.run_job', '--archive-id', str(archive_config['id'])]
        with open(log_path, 'ab') as fh:
            subprocess.Popen(cmd, stdout=fh, stderr=fh, start_new_session=True)
        print(f"[Scheduler] Enqueued scheduled archive {archive_config['name']}")
    except Exception as e:
        print(f"[Scheduler] Archive failed: {e}")
        from app.notifications import send_error_notification
        send_error_notification(archive_config['name'], str(e))


def get_next_run_time(archive_id):
    """Get next run time for a scheduled archive."""
    global scheduler
    
    # If the scheduler isn't initialized (e.g., skipped at import-time), try to initialize it now.
    if scheduler is None:
        try:
            init_scheduler()
        except Exception:
            pass
        if scheduler is None:
            # Still unavailable — we cannot determine next run time.
            return None
    
    job = scheduler.get_job(f"archive_{archive_id}")
    if job and job.next_run_time:
        try:
            # Normalize to UTC-naive datetime for consistent display handling in templates
            from datetime import timezone
            next_run = job.next_run_time.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            # Fallback: strip tzinfo if present
            nr = job.next_run_time
            try:
                next_run = nr.replace(tzinfo=None)
            except Exception:
                next_run = nr
        # Debug log to help trace why the dashboard may show nothing
        try:
            print(f"[Scheduler] Next run for archive_{archive_id}: {next_run.isoformat()}")
        except Exception:
            print(f"[Scheduler] Next run for archive_{archive_id}: {next_run}")
        return next_run
    return None

"""
Scheduler for automatic archive jobs.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.db import get_db
from app.executor import ArchiveExecutor
from app.notifications import get_setting


scheduler = None


def init_scheduler():
    """Initialize and start the scheduler."""
    global scheduler
    
    if scheduler is not None:
        return scheduler
    
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.start()
    
    # Load all scheduled archives
    reload_schedules()
    
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
    
    print("[Scheduler] Initialized and started")
    return scheduler


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
        executor = ArchiveExecutor(archive_config, is_dry_run=False)
        executor.run(triggered_by='schedule')
    except Exception as e:
        print(f"[Scheduler] Archive failed: {e}")
        from app.notifications import send_error_notification
        send_error_notification(archive_config['name'], str(e))


def get_next_run_time(archive_id):
    """Get next run time for a scheduled archive."""
    global scheduler
    
    if scheduler is None:
        return None
    
    job = scheduler.get_job(f"archive_{archive_id}")
    if job and job.next_run_time:
        return job.next_run_time
    return None

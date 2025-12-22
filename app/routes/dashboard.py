"""
Dashboard routes (moved from main.py).
"""
import os
from flask import Blueprint, render_template
from app.auth import login_required, get_current_user
from app.db import get_db
from app.stacks import get_visible_stacks
from app.scheduler import get_next_run_time, get_prev_run_time
from datetime import datetime, timezone
from app.utils import format_bytes, format_duration, get_disk_usage, to_iso_z
from app.notifications import get_setting

bp = Blueprint('dashboard', __name__, url_prefix='/')


@bp.route('/')
@login_required
def index():
    """Dashboard page (moved from main)."""
    # Ensure maintenance flag
    maintenance_mode = get_setting('maintenance_mode', 'false').lower() == 'true'

    # Disk usage
    disk = get_disk_usage()

    # Get stacks for modals (exclude local app stack)
    stacks = get_visible_stacks()

    # Get archives and compute stats
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM archives ORDER BY name;")
        archives_list = cur.fetchall()

        total_archives_configured = len(archives_list)
        active_schedules = sum(1 for a in archives_list if a['schedule_enabled'])

        # Last 24h jobs
        cur.execute("""
            SELECT COUNT(*) as count FROM jobs 
            WHERE job_type = 'archive' 
            AND status = 'success' 
            AND start_time >= NOW() - INTERVAL '24 hours';
        """)
        jobs_last_24h = cur.fetchone()['count']

        # Next scheduled job will be computed after archives are enriched (to detect overdue runs)
        next_scheduled = None

        # Total archives size
        cur.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN job_type = 'archive' THEN total_size_bytes ELSE 0 END),0) as total_archived,
                COALESCE(SUM(reclaimed_bytes),0) as total_reclaimed
            FROM jobs
            WHERE status = 'success';
        """)
        result = cur.fetchone()
        total_archives_size = (result['total_archived'] - result['total_reclaimed']) if result else 0

        # Optionally calculate on-disk size (toggle with environment variable SHOW_ONDISK_ARCHIVE_SIZE)
        on_disk_archives_size = None
        try:
            if os.environ.get('SHOW_ONDISK_ARCHIVE_SIZE') == '1':
                # Use canonical archives path from utils
                archives_path = get_archives_path()
                total = 0
                for root, dirs, files in os.walk(archives_path):
                    for f in files:
                        try:
                            fp = os.path.join(root, f)
                            total += os.path.getsize(fp)
                        except Exception:
                            pass
                on_disk_archives_size = total
        except Exception:
            on_disk_archives_size = None

        # Enrich archives
        archive_list = []
        for archive in archives_list:
            archive_dict = dict(archive)

            # Job count
            cur.execute(
                "SELECT COUNT(*) as count FROM jobs WHERE archive_id = %s AND job_type = 'archive';",
                (archive['id'],)
            )
            archive_dict['job_count'] = cur.fetchone()['count']

            # Last job
            cur.execute("""
                SELECT start_time, status FROM jobs 
                WHERE archive_id = %s AND job_type = 'archive'
                ORDER BY start_time DESC LIMIT 1;
            """, (archive['id'],))
            last_job = cur.fetchone()
            archive_dict['last_run'] = last_job['start_time'] if last_job else None
            archive_dict['last_status'] = last_job['status'] if last_job else None

            # Next run and overdue detection
            if archive['schedule_enabled']:
                next_run = get_next_run_time(archive['id'])
                prev_run = None
                try:
                    prev_run = get_prev_run_time(archive['id'])
                except Exception:
                    prev_run = None

                # Determine overdue: previous scheduled time has passed and no job ran since then
                # Normalize times to timezone-aware UTC datetimes for safe comparison
                now_utc = datetime.now(timezone.utc)
                last_run = archive_dict.get('last_run')
                last_run_utc = None
                if last_run:
                    try:
                        last_run_utc = last_run.replace(tzinfo=timezone.utc)
                    except Exception:
                        # If already tz-aware, convert to UTC
                        try:
                            last_run_utc = last_run.astimezone(timezone.utc)
                        except Exception:
                            last_run_utc = None
                is_overdue = False
                if prev_run:
                    try:
                        prev_run_utc = prev_run.astimezone(timezone.utc)
                    except Exception:
                        prev_run_utc = prev_run
                    if prev_run_utc < now_utc:
                        if not last_run_utc or last_run_utc < prev_run_utc:
                            is_overdue = True

                # Display previous run time when overdue, otherwise show next run
                archive_dict['is_overdue'] = is_overdue
                archive_dict['next_run'] = next_run
                # Keep a record of the missed run when overdue, but always display the upcoming next run
                archive_dict['missed_run'] = prev_run if is_overdue else None
                archive_dict['next_run_display'] = next_run
            else:
                archive_dict['next_run'] = None
                archive_dict['next_run_display'] = None
                archive_dict['is_overdue'] = False

            # Get total archived and reclaimed for this archive
            cur.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN job_type = 'archive' THEN total_size_bytes ELSE 0 END), 0) AS total_archived,
                    COALESCE(SUM(reclaimed_bytes), 0) AS total_reclaimed
                FROM jobs
                WHERE archive_id = %s AND status = 'success';
            """, (archive['id'],))
            res = cur.fetchone()
            archive_dict['total_size'] = (res['total_archived'] - res['total_reclaimed']) if res else 0

            archive_list.append(archive_dict)

        # Recent jobs
        cur.execute("""
            SELECT j.*, a.name as archive_name, a.stacks as archive_stacks,
                   (SELECT STRING_AGG(stack_name, ',') FROM job_stack_metrics WHERE job_id = j.id) as stack_names,
                   CASE WHEN j.status = 'running' THEN NULL ELSE EXTRACT(EPOCH FROM (j.end_time - j.start_time))::integer END as duration_seconds
            FROM jobs j
            LEFT JOIN archives a ON j.archive_id = a.id
            ORDER BY j.start_time DESC
            LIMIT 10;
        """)
        recent_jobs = cur.fetchall()

    # Determine overall next scheduled (pick the earliest upcoming next_run)
    next_dt = None
    for a in archive_list:
        dt = a.get('next_run')
        if not dt:
            continue
        if not next_dt or dt < next_dt:
            next_dt = dt

    if next_dt:
        next_scheduled = {'time': next_dt, 'overdue': False}
    else:
        next_scheduled = None

    # Disk health
    disk_percent = disk.get('percent', 0)
    if disk_percent >= 90:
        disk_status = 'danger'
    elif disk_percent >= 70:
        disk_status = 'warning'
    else:
        disk_status = 'success'

    return render_template(
        'index.html',
        archives=archive_list,
        stacks=stacks,
        recent_jobs=recent_jobs,
        disk=disk,
        disk_status=disk_status,
        total_archives_size=total_archives_size,
        on_disk_archives_size=on_disk_archives_size,
        total_archives_configured=total_archives_configured,
        active_schedules=active_schedules,
        jobs_last_24h=jobs_last_24h,
        next_scheduled=next_scheduled,
        maintenance_mode=maintenance_mode,
        format_bytes=format_bytes,
        format_duration=format_duration,
        current_user=get_current_user()
    )
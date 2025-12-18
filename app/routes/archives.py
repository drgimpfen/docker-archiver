"""
Archives management routes.
"""
import threading
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from app.auth import login_required, get_current_user
from app.db import get_db
from app.stacks import discover_stacks
from app.executor import ArchiveExecutor
from app.scheduler import reload_schedules, get_next_run_time
from app.utils import format_bytes, format_duration, get_disk_usage
from app.notifications import get_setting


bp = Blueprint('archives', __name__, url_prefix='/archives')


@bp.route('/')
@login_required
def list_archives():
    """Archives management page."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM archives ORDER BY name;")
        archive_list = cur.fetchall()
    
    # Get available stacks
    stacks = discover_stacks()
    
    # Enrich archives with next run time
    for archive in archive_list:
        if archive['schedule_enabled'] and archive['schedule_cron']:
            archive['next_run'] = get_next_run_time(archive['id'])
        else:
            archive['next_run'] = None
    
    return render_template(
        'archives.html',
        archives=archive_list,
        stacks=stacks,
        current_user=get_current_user()
    )


@bp.route('/create', methods=['POST'])
@login_required
def create():
    """Create new archive configuration."""
    try:
        from app.security import validate_archive_name
        from croniter import croniter
        
        name = request.form.get('name')
        
        # Validate archive name for security
        if not validate_archive_name(name):
            flash('Invalid archive name. Must be alphanumeric, no special characters or path traversal attempts.', 'danger')
            return redirect(url_for('archives.list_archives'))
        
        # Check if archive name already exists
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM archives WHERE name = %s;", (name,))
            if cur.fetchone():
                flash(f'Archive name "{name}" already exists. Please choose a different name.', 'danger')
                return redirect(url_for('archives.list_archives'))
        
        stacks = request.form.getlist('stacks')
        stop_containers = request.form.get('stop_containers') == 'on'
        schedule_enabled = request.form.get('schedule_enabled') == 'on'
        schedule_cron = request.form.get('schedule_cron', '').strip()
        
        # Validate cron expression if scheduling is enabled
        if schedule_enabled and schedule_cron:
            if not croniter.is_valid(schedule_cron):
                flash('Invalid cron expression. Please use a valid cron format (e.g., "0 3 * * *").', 'danger')
                return redirect(url_for('archives.list_archives'))
        elif schedule_enabled and not schedule_cron:
            flash('Schedule is enabled but no cron expression provided.', 'danger')
            return redirect(url_for('archives.list_archives'))
        output_format = request.form.get('output_format', 'tar')
        
        # Retention settings
        keep_days = int(request.form.get('keep_days', 7))
        keep_weeks = int(request.form.get('keep_weeks', 4))
        keep_months = int(request.form.get('keep_months', 6))
        keep_years = int(request.form.get('keep_years', 2))
        one_per_day = request.form.get('one_per_day') == 'on'
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO archives (
                    name, stacks, stop_containers, schedule_enabled, schedule_cron,
                    output_format, retention_keep_days, retention_keep_weeks,
                    retention_keep_months, retention_keep_years, retention_one_per_day
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
            """, (
                name, stacks, stop_containers, schedule_enabled, schedule_cron,
                output_format, keep_days, keep_weeks, keep_months, keep_years, one_per_day
            ))
            archive_id = cur.fetchone()['id']
            conn.commit()
        
        # Reload scheduler
        reload_schedules()
        
        flash(f'Archive "{name}" created successfully!', 'success')
        return redirect(url_for('archives.list_archives'))
        
    except Exception as e:
        flash(f'Error creating archive: {e}', 'danger')
        return redirect(url_for('archives.list_archives'))


@bp.route('/<int:archive_id>/edit', methods=['POST'])
@login_required
def edit(archive_id):
    """Edit archive configuration."""
    try:
        from croniter import croniter
        
        # Note: name field is ignored - archives cannot be renamed
        stacks = request.form.getlist('stacks')
        stop_containers = request.form.get('stop_containers') == 'on'
        schedule_enabled = request.form.get('schedule_enabled') == 'on'
        schedule_cron = request.form.get('schedule_cron', '').strip()
        
        # Validate cron expression if scheduling is enabled
        if schedule_enabled and schedule_cron:
            if not croniter.is_valid(schedule_cron):
                flash('Invalid cron expression. Please use a valid cron format (e.g., "0 3 * * *").', 'danger')
                return redirect(url_for('archives.list_archives'))
        elif schedule_enabled and not schedule_cron:
            flash('Schedule is enabled but no cron expression provided.', 'danger')
            return redirect(url_for('archives.list_archives'))
        output_format = request.form.get('output_format', 'tar')
        
        keep_days = int(request.form.get('keep_days', 7))
        keep_weeks = int(request.form.get('keep_weeks', 4))
        keep_months = int(request.form.get('keep_months', 6))
        keep_years = int(request.form.get('keep_years', 2))
        one_per_day = request.form.get('one_per_day') == 'on'
        
        with get_db() as conn:
            cur = conn.cursor()
            # Get current name for success message
            cur.execute("SELECT name FROM archives WHERE id = %s;", (archive_id,))
            archive = cur.fetchone()
            archive_name = archive['name'] if archive else 'Archive'
            
            cur.execute("""
                UPDATE archives SET
                    stacks = %s, stop_containers = %s,
                    schedule_enabled = %s, schedule_cron = %s, output_format = %s,
                    retention_keep_days = %s, retention_keep_weeks = %s,
                    retention_keep_months = %s, retention_keep_years = %s,
                    retention_one_per_day = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
            """, (
                stacks, stop_containers, schedule_enabled, schedule_cron,
                output_format, keep_days, keep_weeks, keep_months, keep_years,
                one_per_day, archive_id
            ))
            conn.commit()
        
        reload_schedules()
        
        flash(f'Archive "{archive_name}" updated successfully!', 'success')
        return redirect(url_for('archives.list_archives'))
        
    except Exception as e:
        flash(f'Error updating archive: {e}', 'danger')
        return redirect(url_for('archives.list_archives'))


@bp.route('/<int:archive_id>/delete', methods=['POST'])
@login_required
def delete(archive_id):
    """Delete archive configuration."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM archives WHERE id = %s;", (archive_id,))
            conn.commit()
        
        reload_schedules()
        
        flash('Archive deleted successfully!', 'success')
        return redirect(url_for('archives.list_archives'))
        
    except Exception as e:
        flash(f'Error deleting archive: {e}', 'danger')
        return redirect(url_for('archives.list_archives'))


@bp.route('/<int:archive_id>/retention', methods=['POST'])
@login_required
def run_retention_only(archive_id):
    """Run retention cleanup manually (without creating new archive)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM archives WHERE id = %s;", (archive_id,))
            archive = cur.fetchone()
        
        if not archive:
            flash('Archive not found', 'danger')
            return redirect(url_for('index'))
        
        # Run retention in background
        def run_retention_job():
            from app.retention import run_retention
            from app.db import get_db
            
            # Create a job record with empty log
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO jobs (archive_id, job_type, status, start_time, triggered_by, log)
                    VALUES (%s, 'retention', 'running', NOW(), 'manual', '')
                    RETURNING id;
                """, (archive_id,))
                job_id = cur.fetchone()['id']
                conn.commit()
            
            # Log function
            def log_message(level, message):
                import datetime
                timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                log_line = f"[{timestamp}] [{level}] {message}\n"
                
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE jobs 
                        SET log = log || %s
                        WHERE id = %s;
                    """, (log_line, job_id))
                    conn.commit()
            
            try:
                log_message('INFO', f"Starting retention cleanup for '{archive['name']}'")
                
                # Run retention with log callback
                reclaimed = run_retention(dict(archive), job_id, is_dry_run=False, log_callback=log_message)
                
                log_message('INFO', f"Retention completed, reclaimed {reclaimed} bytes")
                
                # Update job status
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE jobs 
                        SET status = 'success', end_time = NOW(), reclaimed_size_bytes = %s
                        WHERE id = %s;
                    """, (reclaimed, job_id))
                    conn.commit()
                
                from app.notifications import send_retention_notification
                send_retention_notification(archive['name'], 0, reclaimed)  # deleted_count not tracked
                
            except Exception as e:
                log_message('ERROR', f"Retention failed: {str(e)}")
                
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE jobs 
                        SET status = 'failed', end_time = NOW(), error_message = %s
                        WHERE id = %s;
                    """, (str(e), job_id))
                    conn.commit()
        
        thread = threading.Thread(target=run_retention_job)
        thread.daemon = True
        thread.start()
        
        flash(f'Retention cleanup started for "{archive["name"]}"', 'success')
        return redirect(url_for('index'))
        
    except Exception as e:
        flash(f'Failed to start retention: {str(e)}', 'danger')
        return redirect(url_for('index'))


@bp.route('/<int:archive_id>/run', methods=['POST'])
@login_required
def run(archive_id):
    """Run archive job manually."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM archives WHERE id = %s;", (archive_id,))
            archive = cur.fetchone()
        
        if not archive:
            flash('Archive not found', 'danger')
            return redirect(url_for('index'))
        
        # Run in background
        def run_job():
            executor = ArchiveExecutor(dict(archive), is_dry_run=False)
            executor.run(triggered_by='manual')
        
        thread = threading.Thread(target=run_job)
        thread.daemon = True
        thread.start()
        
        flash(f'Archive job for "{archive["name"]}" started!', 'info')
        return redirect(url_for('index'))
        
    except Exception as e:
        flash(f'Error starting archive: {e}', 'danger')
        return redirect(url_for('index'))


@bp.route('/<int:archive_id>/dry-run', methods=['POST'])
@login_required
def dry_run(archive_id):
    """Run archive job in dry-run mode."""
    try:
        stop_containers = request.form.get('dry_stop_containers') == 'on'
        create_archive = request.form.get('dry_create_archive') == 'on'
        run_retention = request.form.get('dry_run_retention') == 'on'
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM archives WHERE id = %s;", (archive_id,))
            archive = cur.fetchone()
        
        if not archive:
            flash('Archive not found', 'danger')
            return redirect(url_for('index'))
        
        dry_run_config = {
            'stop_containers': stop_containers,
            'create_archive': create_archive,
            'run_retention': run_retention
        }
        
        def run_job():
            executor = ArchiveExecutor(dict(archive), is_dry_run=True, dry_run_config=dry_run_config)
            executor.run(triggered_by='manual_dry_run')
        
        thread = threading.Thread(target=run_job)
        thread.daemon = True
        thread.start()
        
        flash(f'Dry run for "{archive["name"]}" started!', 'info')
        return redirect(url_for('index'))
        
    except Exception as e:
        flash(f'Error starting dry run: {e}', 'danger')
        return redirect(url_for('index'))

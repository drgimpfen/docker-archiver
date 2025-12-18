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
        name = request.form.get('name')
        stacks = request.form.getlist('stacks')
        stop_containers = request.form.get('stop_containers') == 'on'
        schedule_enabled = request.form.get('schedule_enabled') == 'on'
        schedule_cron = request.form.get('schedule_cron', '')
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
        name = request.form.get('name')
        stacks = request.form.getlist('stacks')
        stop_containers = request.form.get('stop_containers') == 'on'
        schedule_enabled = request.form.get('schedule_enabled') == 'on'
        schedule_cron = request.form.get('schedule_cron', '')
        output_format = request.form.get('output_format', 'tar')
        
        keep_days = int(request.form.get('keep_days', 7))
        keep_weeks = int(request.form.get('keep_weeks', 4))
        keep_months = int(request.form.get('keep_months', 6))
        keep_years = int(request.form.get('keep_years', 2))
        one_per_day = request.form.get('one_per_day') == 'on'
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE archives SET
                    name = %s, stacks = %s, stop_containers = %s,
                    schedule_enabled = %s, schedule_cron = %s, output_format = %s,
                    retention_keep_days = %s, retention_keep_weeks = %s,
                    retention_keep_months = %s, retention_keep_years = %s,
                    retention_one_per_day = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
            """, (
                name, stacks, stop_containers, schedule_enabled, schedule_cron,
                output_format, keep_days, keep_weeks, keep_months, keep_years,
                one_per_day, archive_id
            ))
            conn.commit()
        
        reload_schedules()
        
        flash(f'Archive "{name}" updated successfully!', 'success')
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

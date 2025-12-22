"""
Archives management routes.
"""
import os
import subprocess
import sys
import threading
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from app.auth import login_required, get_current_user
from app.db import get_db
from app.stacks import get_visible_stacks
from app.executor import ArchiveExecutor
from app.scheduler import reload_schedules, get_next_run_time, publish_reload_signal
from app import utils
from app.notifications import get_setting, send_retention_notification
from app.utils import setup_logging, get_logger, get_jobs_log_dir, format_bytes, format_duration, get_disk_usage, to_iso_z, now, local_now, filename_safe

# Configure logging using centralized setup so LOG_LEVEL is respected
setup_logging()
logger = get_logger(__name__)

bp = Blueprint('archives', __name__, url_prefix='/api/archives')

# Legacy blueprint to preserve old `/archives` paths and provide safe redirects.
# For POST endpoints we use 307 redirects to preserve the HTTP method and body when
# clients still post to the old paths. The primary implementation of the handlers
# remains under the `/api/archives` prefix (the `archives` blueprint above).
legacy_bp = Blueprint('archives_legacy', __name__, url_prefix='/archives')

@legacy_bp.route('/', methods=['GET'])
@login_required
def legacy_index():
    """Redirect legacy archive UI path to the Dashboard (UI was removed)."""
    return redirect(url_for('dashboard.index'))

# Preserve POST behavior via 307 redirects to the new API endpoints (preserves method)
@legacy_bp.route('/create', methods=['POST'])
@login_required
def legacy_create():
    return redirect(url_for('archives.create'), code=307)

@legacy_bp.route('/<int:archive_id>/edit', methods=['POST'])
@login_required
def legacy_edit(archive_id):
    return redirect(url_for('archives.edit', archive_id=archive_id), code=307)

@legacy_bp.route('/<int:archive_id>/delete', methods=['POST'])
@login_required
def legacy_delete(archive_id):
    return redirect(url_for('archives.delete', archive_id=archive_id), code=307)

@legacy_bp.route('/<int:archive_id>/retention', methods=['POST'])
@login_required
def legacy_retention(archive_id):
    return redirect(url_for('archives.run_retention_only', archive_id=archive_id), code=307)


def _is_ajax_request():
    """Return True if the request appears to be an AJAX/json request."""
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')


def _enrich_archive(cur, archive):
    """Enrich a single archive row (dict-like) with additional computed fields used by the UI."""

    archive_dict = dict(archive)

    # Job count
    cur.execute("SELECT COUNT(*) as count FROM jobs WHERE archive_id = %s AND job_type = 'archive';", (archive['id'],))
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

        now_utc = datetime.now(timezone.utc)
        last_run = archive_dict.get('last_run')
        last_run_utc = None
        if last_run:
            try:
                last_run_utc = last_run.replace(tzinfo=timezone.utc)
            except Exception:
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

        archive_dict['is_overdue'] = is_overdue
        archive_dict['next_run'] = next_run
        # Keep a record of the missed run when overdue, but always display the upcoming next run
        archive_dict['missed_run'] = prev_run if is_overdue else None
        archive_dict['next_run_display'] = next_run
    else:
        archive_dict['next_run'] = None
        archive_dict['next_run_display'] = None
        archive_dict['is_overdue'] = False

    # Total archived size for this archive
    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN job_type = 'archive' THEN total_size_bytes ELSE 0 END), 0) AS total_archived,
            COALESCE(SUM(reclaimed_bytes), 0) AS total_reclaimed
        FROM jobs
        WHERE archive_id = %s AND status = 'success';
    """, (archive['id'],))
    res = cur.fetchone()
    archive_dict['total_size'] = (res['total_archived'] - res['total_reclaimed']) if res else 0

    return archive_dict


# The archive listing UI has been removed — archive creation/edit/deletion
# are handled on the Dashboard. The routes for create/edit/delete remain in place
# so the Dashboard can POST to them directly via AJAX or forms.
# (list_archives was intentionally removed per request.)


@bp.route('/create', methods=['POST'])
@login_required
def create():
    """Create new archive configuration."""
    try:
        from app.security import validate_archive_name
        
        name = request.form.get('name')
        
        # Validate archive name for security
        if not validate_archive_name(name):
            msg = 'Invalid archive name. Must be alphanumeric, no special characters or path traversal attempts.'
            if _is_ajax_request():
                return jsonify({'status': 'error', 'message': msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('dashboard.index'))
        
        # Check if archive name already exists
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM archives WHERE name = %s;", (name,))
            if cur.fetchone():
                msg = f'Archive name "{name}" already exists. Please choose a different name.'
                if _is_ajax_request():
                    return jsonify({'status': 'error', 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('dashboard.index'))
        
        stacks = request.form.getlist('stacks')
        # Require at least one stack when creating an archive
        if not stacks:
            msg = 'Please select at least one stack for the archive.'
            if _is_ajax_request():
                return jsonify({'status': 'error', 'message': msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('dashboard.index'))

        # Validate selected stacks are visible and not the local application stack (self-backup)
        try:
            visible = get_visible_stacks()
            visible_names = {s['name'] for s in visible}
            # Reject any selections that don't correspond to a visible stack (prevents crafted requests)
            for selected in stacks:
                if selected not in visible_names:
                    msg = 'Selected stack is not available.'
                    if _is_ajax_request():
                        return jsonify({'status': 'error', 'message': msg}), 400
                    flash(msg, 'danger')
                    return redirect(url_for('dashboard.index'))
        except Exception:
            # If validation fails for unexpected reasons, block to be safe
            msg = 'Stack validation failed; please try again or contact the administrator.'
            if _is_ajax_request():
                return jsonify({'status': 'error', 'message': msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('dashboard.index'))
        stop_containers = request.form.get('stop_containers') == 'on'
        schedule_enabled = request.form.get('schedule_enabled') == 'on'
        schedule_cron = request.form.get('schedule_cron', '').strip()
        
        # Validate cron expression if scheduling is enabled
        if schedule_enabled and schedule_cron:
            if not croniter.is_valid(schedule_cron):
                flash('Invalid cron expression. Please use a valid cron format (e.g., "0 3 * * *").', 'danger')
                return redirect(url_for('dashboard.index'))
        elif schedule_enabled and not schedule_cron:
            flash('Schedule is enabled but no cron expression provided.', 'danger')
            return redirect(url_for('dashboard.index'))
        output_format = request.form.get('output_format', 'tar')
        
        # Retention settings
        def parse_retention_field(name, default):
            raw = request.form.get(name)
            if raw is None:
                return default
            raw = str(raw).strip()
            if raw == '':
                return 0
            try:
                val = int(raw)
                return val if val >= 0 else 0
            except ValueError:
                raise ValueError(f"Invalid integer for {name}: '{raw}'")

        try:
            keep_days = parse_retention_field('keep_days', 7)
            keep_weeks = parse_retention_field('keep_weeks', 4)
            keep_months = parse_retention_field('keep_months', 6)
            keep_years = parse_retention_field('keep_years', 2)
        except ValueError as ve:
            flash(str(ve), 'danger')
            return redirect(url_for('dashboard.index'))

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
        try:
            publish_reload_signal()
        except Exception:
            pass
        
        if _is_ajax_request():
            # Return rendered archive card so client can insert it without a full page reload
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM archives WHERE id = %s;", (archive_id,))
                new_archive = cur.fetchone()
                archive_dict = _enrich_archive(cur, new_archive)
            rendered = render_template('_archive_card.html', archive=archive_dict, stacks=get_visible_stacks(), current_user=get_current_user(), format_bytes=format_bytes)
            # Wrap in a column container that matches the dashboard grid
            wrapped = f"<div class=\"col-md-6 col-lg-3\" id=\"archive-card-{archive_id}\">" + rendered + "</div>"
            archive_resp = dict(archive_dict)
            archive_resp['last_run'] = to_iso_z(archive_resp.get('last_run'))
            archive_resp['next_run'] = to_iso_z(archive_resp.get('next_run'))
            archive_resp['next_run_display'] = to_iso_z(archive_resp.get('next_run_display'))
            archive_resp['missed_run'] = to_iso_z(archive_resp.get('missed_run'))
            archive_resp['is_overdue'] = bool(archive_resp.get('is_overdue'))
            return jsonify({'status': 'success', 'html': wrapped, 'archive_id': archive_id, 'archive': archive_resp})

        flash(f'Archive "{name}" created successfully!', 'success')
        return redirect(url_for('dashboard.index'))
        
    except Exception as e:
        if _is_ajax_request():
            return jsonify({'status': 'error', 'message': str(e)}), 500
        flash(f'Error creating archive: {e}', 'danger')
        return redirect(url_for('dashboard.index'))


@bp.route('/<int:archive_id>/edit', methods=['POST'])
@login_required
def edit(archive_id):
    """Edit archive configuration."""
    try:
        
        # Note: name field is ignored - archives cannot be renamed
        stacks = request.form.getlist('stacks')
        # Require at least one stack when editing an archive
        if not stacks:
            msg = 'Please select at least one stack for the archive.'
            if _is_ajax_request():
                return jsonify({'status': 'error', 'message': msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('dashboard.index'))
        stop_containers = request.form.get('stop_containers') == 'on'
        schedule_enabled = request.form.get('schedule_enabled') == 'on'
        schedule_cron = request.form.get('schedule_cron', '').strip()
        
        # Validate cron expression if scheduling is enabled
        if schedule_enabled and schedule_cron:
            if not croniter.is_valid(schedule_cron):
                msg = 'Invalid cron expression. Please use a valid cron format (e.g., "0 3 * * *").'
                if _is_ajax_request():
                    return jsonify({'status': 'error', 'message': msg}), 400
                flash(msg, 'danger')
                return redirect(url_for('dashboard.index'))
        elif schedule_enabled and not schedule_cron:
            msg = 'Schedule is enabled but no cron expression provided.'
            if _is_ajax_request():
                return jsonify({'status': 'error', 'message': msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('dashboard.index'))
        output_format = request.form.get('output_format', 'tar')
        
        def parse_retention_field(name, default):
            raw = request.form.get(name)
            if raw is None:
                return default
            raw = str(raw).strip()
            if raw == '':
                return 0
            try:
                val = int(raw)
                return val if val >= 0 else 0
            except ValueError:
                raise ValueError(f"Invalid integer for {name}: '{raw}'")

        try:
            keep_days = parse_retention_field('keep_days', 7)
            keep_weeks = parse_retention_field('keep_weeks', 4)
            keep_months = parse_retention_field('keep_months', 6)
            keep_years = parse_retention_field('keep_years', 2)
        except ValueError as ve:
            msg = str(ve)
            if _is_ajax_request():
                return jsonify({'status': 'error', 'message': msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('dashboard.index'))

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

        if _is_ajax_request():
            # Return updated rendered card so client can replace the card without full reload
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM archives WHERE id = %s;", (archive_id,))
                updated_archive = cur.fetchone()
                archive_dict = _enrich_archive(cur, updated_archive)
            rendered = render_template('_archive_card.html', archive=archive_dict, stacks=get_visible_stacks(), current_user=get_current_user(), format_bytes=format_bytes)
            # Wrap in a column container that matches the dashboard grid
            wrapped = f"<div class=\"col-md-6 col-lg-3\" id=\"archive-card-{archive_id}\">" + rendered + "</div>"
            archive_resp = dict(archive_dict)
            archive_resp['last_run'] = to_iso_z(archive_resp.get('last_run'))
            archive_resp['next_run'] = to_iso_z(archive_resp.get('next_run'))
            archive_resp['next_run_display'] = to_iso_z(archive_resp.get('next_run_display'))
            archive_resp['missed_run'] = to_iso_z(archive_resp.get('missed_run'))
            archive_resp['is_overdue'] = bool(archive_resp.get('is_overdue'))
            return jsonify({'status': 'success', 'html': wrapped, 'archive_id': archive_id, 'archive': archive_resp})

        flash(f'Archive "{archive_name}" updated successfully!', 'success')
        return redirect(url_for('dashboard.index'))
        
    except Exception as e:
        if _is_ajax_request():
            return jsonify({'status': 'error', 'message': str(e)}), 500
        flash(f'Error updating archive: {e}', 'danger')
        return redirect(url_for('dashboard.index'))


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
        try:
            publish_reload_signal()
        except Exception:
            pass
        
        if _is_ajax_request():
            return jsonify({'status': 'success', 'archive_id': archive_id}), 200

        flash('Archive deleted successfully!', 'success')
        return redirect(url_for('dashboard.index'))
        
    except Exception as e:
        if _is_ajax_request():
            return jsonify({'status': 'error', 'message': str(e)}), 500
        flash(f'Error deleting archive: {e}', 'danger')
        return redirect(url_for('dashboard.index'))


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
            return redirect(url_for('dashboard.index'))
        
        # Convert to dict to avoid database connection issues in thread
        archive_dict = dict(archive)
        
        # Run retention in background
        def run_retention_job():
            logger.info("Retention thread started for archive_id=%s", archive_id)
            try:
                from app.retention import run_retention
                
                # Create a job record with empty log
                with get_db() as conn:
                    cur = conn.cursor()
                    start_time = now()
                    cur.execute("""
                        INSERT INTO jobs (archive_id, job_type, status, start_time, triggered_by, log)
                        VALUES (%s, 'retention', 'running', %s, 'manual', '')
                        RETURNING id;
                    """, (archive_id, start_time))
                    job_id = cur.fetchone()['id']
                    conn.commit()
            except Exception as e:
                logger.exception("Failed to create retention job record: %s", e)
                return
            
            # Log function
            def log_message(level, message):
                timestamp = local_now().strftime('%Y-%m-%d %H:%M:%S')
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
                log_message('INFO', f"Starting retention cleanup for '{archive_dict['name']}'")
                
                # Run retention with log callback
                reclaimed = run_retention(archive_dict, job_id, is_dry_run=False, log_callback=log_message)
                
                log_message('INFO', f"Retention completed, reclaimed {reclaimed} bytes")
                
                # Update job status
                with get_db() as conn:
                    cur = conn.cursor()
                    end_time = now()
                    cur.execute("""
                        UPDATE jobs 
                        SET status = 'success', end_time = %s, reclaimed_size_bytes = %s
                        WHERE id = %s;
                    """, (end_time, reclaimed, job_id))
                    conn.commit()
                
                send_retention_notification(archive['name'], 0, reclaimed)  # deleted_count not tracked
                
            except Exception as e:
                logger.exception("Manual retention run failed: %s", e)
                log_message('ERROR', f"Retention failed: {str(e)}")
                
                with get_db() as conn:
                    cur = conn.cursor()
                    end_time = now()
                    cur.execute("""
                        UPDATE jobs 
                        SET status = 'failed', end_time = %s, error_message = %s
                        WHERE id = %s;
                    """, (end_time, str(e), job_id))
                    conn.commit()
        
        logger.info("Creating retention thread for archive: %s", archive_dict['name'])
        thread = threading.Thread(target=run_retention_job)
        thread.daemon = True
        thread.start()
        logger.info("Retention thread started: %s", thread.is_alive())
        
        flash(f'Retention cleanup started for "{archive_dict["name"]}"', 'success')
        return redirect(url_for('dashboard.index'))
        
    except Exception as e:
        logger.exception("Failed to start retention route: %s", e)
        flash(f'Failed to start retention: {str(e)}', 'danger')
        return redirect(url_for('dashboard.index'))


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
            return redirect(url_for('dashboard.index'))
        
        # Create a job record atomically to prevent duplicate starts
        with get_db() as conn:
            cur = conn.cursor()
            start_time = now()
            cur.execute("""
                INSERT INTO jobs (archive_id, job_type, status, start_time, triggered_by, log)
                SELECT %s, 'archive', 'running', %s, 'manual', ''
                WHERE NOT EXISTS (
                    SELECT 1 FROM jobs WHERE archive_id = %s AND status = 'running'
                )
                RETURNING id;
            """, (archive_id, start_time, archive_id))
            row = cur.fetchone()
            if not row:
                flash('Archive already has a running job', 'warning')
                return redirect(url_for('dashboard.index'))
            job_id = row['id']
            conn.commit()

        # Start archive as detached subprocess and log to file
        import sys
        jobs_dir = get_jobs_log_dir()
        os.makedirs(jobs_dir, exist_ok=True)
        log_path = os.path.join(jobs_dir, f"archive_{archive_id}.log")
        cmd = [sys.executable, '-m', 'app.run_job', '--archive-id', str(archive_id), '--job-id', str(job_id)]
        try:
            with open(log_path, 'ab') as fh:
                subprocess.Popen(cmd, stdout=fh, stderr=fh, start_new_session=True)
            flash(f'Archive job for "{archive["name"]}" started', 'info')
        except Exception as e:
            flash(f'Failed to start job: {e}', 'danger')
        return redirect(url_for('dashboard.index'))
        
    except Exception as e:
        flash(f'Error starting archive: {e}', 'danger')
        return redirect(url_for('dashboard.index'))


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
            return redirect(url_for('dashboard.index'))
        
        dry_run_config = {
            'stop_containers': stop_containers,
            'create_archive': create_archive,
            'run_retention': run_retention
        }
        
        cmd = [sys.executable, '-m', 'app.run_job', '--archive-id', str(archive_id), '--dry-run']
        if not stop_containers:
            cmd.append('--no-stop-containers')
        if not create_archive:
            cmd.append('--no-create-archive')
        if not run_retention:
            cmd.append('--no-run-retention')

        jobs_dir = get_jobs_log_dir()
        os.makedirs(jobs_dir, exist_ok=True)
        timestamp = utils.local_now().strftime('%Y%m%d_%H%M%S')
        safe_name = utils.filename_safe(archive['name'])
        log_name = f"{timestamp}_dryrun_{safe_name}.log"
        log_path = os.path.join(jobs_dir, log_name)
        try:
            subprocess.Popen(cmd + ['--log-path', log_path], start_new_session=True)
            flash(f'Dry run for "{archive["name"]}" started (log: {log_name})', 'info')
        except Exception as e:
            flash(f'Failed to start dry run: {e}', 'danger')

        return redirect(url_for('dashboard.index'))
        
    except Exception as e:
        flash(f'Error starting dry run: {e}', 'danger')
        return redirect(url_for('dashboard.index'))

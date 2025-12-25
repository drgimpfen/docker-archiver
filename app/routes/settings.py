"""
Settings routes.
"""
import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from app.auth import login_required, get_current_user, hash_password, verify_password
from app.db import get_db
from app.scheduler import reload_schedules
from app.notifications import send_test_notification
from app.utils import format_mode


bp = Blueprint('settings', __name__, url_prefix='/settings')




@bp.route('/', methods=['GET', 'POST'])
@login_required
def manage_settings():
    """Settings overview / General settings page."""
    if request.method == 'POST':
        try:
            base_url = request.form.get('base_url', 'http://localhost:8080')
            maintenance_mode = request.form.get('maintenance_mode') == 'on'

            with get_db() as conn:
                cur = conn.cursor()
                settings_to_update = [
                    ('base_url', base_url),
                    ('maintenance_mode', 'true' if maintenance_mode else 'false'),
                ]
                for key, value in settings_to_update:
                    cur.execute("""
                        INSERT INTO settings (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP;
                    """, (key, value, value))
                conn.commit()

            reload_schedules()
            try:
                from app.scheduler import publish_reload_signal
                publish_reload_signal()
            except Exception:
                pass

            flash('Settings saved successfully!', 'success')
            return redirect(url_for('settings.manage_settings'))
        except Exception as e:
            flash(f'Error saving settings: {e}', 'danger')

    # Load current settings
    settings_dict = {}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings;")
        for row in cur.fetchall():
            settings_dict[row['key']] = row['value']

    return render_template('settings_general.html', settings=settings_dict, current_user=get_current_user())


@bp.route('/notifications', methods=['GET', 'POST'])
@login_required
def manage_notifications():
    """Notifications settings page."""
    if request.method == 'POST':
        try:
            notification_subject_tag = request.form.get('notification_subject_tag', '')
            notify_success = request.form.get('notify_success') == 'on'
            notify_error = request.form.get('notify_error') == 'on'
            notify_attach_log = request.form.get('notify_attach_log') == 'on'
            notify_attach_log_on_failure = request.form.get('notify_attach_log_on_failure') == 'on'

            # SMTP settings
            smtp_server = request.form.get('smtp_server', '').strip()
            smtp_port = request.form.get('smtp_port', '').strip()
            smtp_user = request.form.get('smtp_user', '').strip()
            smtp_password = request.form.get('smtp_password', '').strip()
            smtp_from = request.form.get('smtp_from', '').strip()
            smtp_use_tls = 'true' if request.form.get('smtp_use_tls') == 'on' else 'false'

            settings_to_update = [
                ('notification_subject_tag', notification_subject_tag),
                ('notify_on_success', 'true' if notify_success else 'false'),
                ('notify_on_error', 'true' if notify_error else 'false'),
                ('notify_attach_log', 'true' if notify_attach_log else 'false'),
                ('notify_attach_log_on_failure', 'true' if notify_attach_log_on_failure else 'false'),
                ('smtp_server', smtp_server),
                ('smtp_port', smtp_port),
                ('smtp_user', smtp_user),
                ('smtp_password', smtp_password),
                ('smtp_from', smtp_from),
                ('smtp_use_tls', smtp_use_tls),
            ]

            with get_db() as conn:
                cur = conn.cursor()
                for key, value in settings_to_update:
                    cur.execute("""
                        INSERT INTO settings (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP;
                    """, (key, value, value))
                conn.commit()

            flash('Notification settings saved successfully!', 'success')
            return redirect(url_for('settings.manage_notifications'))
        except Exception as e:
            flash(f'Error saving notification settings: {e}', 'danger')

    # Load current settings
    settings_dict = {}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings;")
        for row in cur.fetchall():
            settings_dict[row['key']] = row['value']

    email_configured = bool(settings_dict.get('smtp_server') and settings_dict.get('smtp_from'))
    return render_template('settings_notifications.html', settings=settings_dict, current_user=get_current_user(), email_configured=email_configured)


@bp.route('/security', methods=['GET', 'POST'])
@login_required
def manage_security():
    """Security-related settings page."""
    if request.method == 'POST':
        try:
            apply_permissions = request.form.get('apply_permissions') == 'on'
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO settings (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP;
                """, ('apply_permissions', 'true' if apply_permissions else 'false', 'true' if apply_permissions else 'false'))
                conn.commit()
            flash('Security settings saved successfully!', 'success')
            return redirect(url_for('settings.manage_security'))
        except Exception as e:
            flash(f'Error saving security settings: {e}', 'danger')

    # Load current settings
    settings_dict = {}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings;")
        for row in cur.fetchall():
            settings_dict[row['key']] = row['value']

    return render_template('settings_security.html', settings=settings_dict, current_user=get_current_user())


@bp.route('/cleanup', methods=['GET', 'POST'])
@login_required
def manage_cleanup():
    """Cleanup/scheduler settings page."""
    if request.method == 'POST':
        try:
            cleanup_enabled = request.form.get('cleanup_enabled') == 'on'
            cleanup_cron = request.form.get('cleanup_cron', '30 2 * * *')
            cleanup_log_retention_days = request.form.get('cleanup_log_retention_days', '90')
            cleanup_dry_run = request.form.get('cleanup_dry_run') == 'on'
            notify_cleanup = request.form.get('notify_cleanup') == 'on'

            # Validate cron expression loosely (5 parts)
            if cleanup_enabled:
                cron_parts = cleanup_cron.split()
                if len(cron_parts) != 5:
                    flash('Invalid cleanup cron expression. Use format: minute hour day month day_of_week (e.g., "30 2 * * *").', 'danger')
                    return redirect(url_for('settings.manage_cleanup'))

            with get_db() as conn:
                cur = conn.cursor()
                settings_to_update = [
                    ('cleanup_enabled', 'true' if cleanup_enabled else 'false'),
                    ('cleanup_cron', cleanup_cron),
                    ('cleanup_log_retention_days', cleanup_log_retention_days),
                    ('cleanup_dry_run', 'true' if cleanup_dry_run else 'false'),
                    ('notify_on_cleanup', 'true' if notify_cleanup else 'false'),
                ]
                for key, value in settings_to_update:
                    cur.execute("""
                        INSERT INTO settings (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP;
                    """, (key, value, value))
                conn.commit()

            from app.scheduler import schedule_cleanup_task
            schedule_cleanup_task()

            flash('Cleanup settings saved successfully!', 'success')
            return redirect(url_for('settings.manage_cleanup'))
        except Exception as e:
            flash(f'Error saving cleanup settings: {e}', 'danger')

    # Load current settings
    settings_dict = {}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings;")
        for row in cur.fetchall():
            settings_dict[row['key']] = row['value']

    return render_template('settings_cleanup.html', settings=settings_dict, current_user=get_current_user())


@bp.route('/test-notification', methods=['POST'])
@login_required
def test_notification():
    """Send a test notification."""
    try:
        send_test_notification()
        return jsonify({'success': True, 'message': 'Test notification sent successfully!'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to send notification: {str(e)}'}), 500


@bp.route('/profile', methods=['GET', 'POST'])
@login_required
def manage_profile():
    """User profile page (now integrated under /settings/profile)."""
    user = get_current_user()
    if request.method == 'POST':
        try:
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip()
            current_password = request.form.get('current_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')

            if not username:
                flash('Username is required', 'danger')
                return redirect(url_for('settings.manage_profile'))

            with get_db() as conn:
                cur = conn.cursor()

                # Check if username is already taken by another user
                if username != user['username']:
                    cur.execute("SELECT id FROM users WHERE username = %s AND id != %s;", (username, user['id']))
                    if cur.fetchone():
                        flash('Username already taken', 'danger')
                        return redirect(url_for('settings.manage_profile'))

                # Update username and email
                cur.execute("""
                    UPDATE users 
                    SET username = %s, email = %s, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = %s;
                """, (username, email, user['id']))

                # Update password if provided
                if new_password:
                    if not current_password:
                        flash('Current password is required to change password', 'danger')
                        conn.rollback()
                        return redirect(url_for('settings.manage_profile'))

                    # Verify current password
                    if not verify_password(current_password, user['password_hash']):
                        flash('Current password is incorrect', 'danger')
                        conn.rollback()
                        return redirect(url_for('settings.manage_profile'))

                    if new_password != confirm_password:
                        flash('New passwords do not match', 'danger')
                        conn.rollback()
                        return redirect(url_for('settings.manage_profile'))

                    if len(new_password) < 6:
                        flash('Password must be at least 6 characters', 'danger')
                        conn.rollback()
                        return redirect(url_for('settings.manage_profile'))

                    # Update password
                    password_hash = hash_password(new_password)
                    cur.execute("""
                        UPDATE users 
                        SET password_hash = %s, updated_at = CURRENT_TIMESTAMP 
                        WHERE id = %s;
                    """, (password_hash, user['id']))

                conn.commit()

                # Update session username if changed
                if username != user['username']:
                    from flask import session
                    session['username'] = username

                flash('Profile updated successfully!', 'success')
                return redirect(url_for('settings.manage_profile'))
        except Exception as e:
            flash(f'Error updating profile: {e}', 'danger')

    return render_template('settings_profile.html', current_user=get_current_user())


@bp.route('/fix-permissions', methods=['POST'])
@login_required
def fix_permissions():
    """Start a background task to apply configured permissions to existing archives."""
    try:
        import threading
        from app.utils import get_archives_path, apply_permissions_recursive, get_logger
        from app.notifications import send_permissions_fix_notification
        logger = get_logger(__name__)

        def _run():
            try:
                base = get_archives_path()
                logger.info("[FixPerm] Starting permission fix on %s", base)
                # Create a temporary report file to store the full list of fixed paths
                import tempfile
                tf = tempfile.NamedTemporaryFile(mode='w', prefix='permissions_fix_', suffix='.txt', delete=False, encoding='utf-8')
                tf_name = tf.name
                tf.close()

                # Write full report to file; avoid keeping large in-memory samples to reduce memory usage.
                res = apply_permissions_recursive(base, collect_list=False, report_path=tf_name)
                logger.info("[FixPerm] Completed: %s", res)

                # Send notification with the result dict and attach full report file
                try:
                    send_permissions_fix_notification(res, report_path=tf_name)
                except Exception:
                    logger.exception('[FixPerm] Failed to send permissions notification')

            except Exception as e:
                logger.exception("[FixPerm] Failed: %s", e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return jsonify({'status': 'started', 'message': 'Fixing permissions started in background. Check logs for progress.'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@bp.route('/check-permissions', methods=['POST'])
@login_required
def check_permissions():
    """Check permissions for files and directories under archives and return a report."""
    try:
        from app.utils import get_archives_path
        import os
        base = get_archives_path()
        file_mode = 0o644
        dir_mode = 0o755

        total_files = 0
        total_dirs = 0
        archives = {}  # key -> {'path': base/key, 'mismatched_files': [], 'mismatched_dirs': [], 'file_count':0, 'dir_count':0}

        # Walk archives path
        for root, dirs, files in os.walk(base):
            for d in dirs:
                total_dirs += 1
                p = os.path.join(root, d)
                try:
                    mode = os.stat(p).st_mode & 0o777
                    if mode != dir_mode:
                        rel = os.path.relpath(p, base)
                        parts = rel.split(os.sep) if rel and not rel.startswith('..') else []
                        top = parts[0] if len(parts) >= 1 else ''
                        stack = parts[1] if len(parts) >= 2 else '<root>'
                        if top not in archives:
                            archives[top] = {'path': os.path.join(base, top), 'stacks': {}, 'mismatched_files': [], 'mismatched_dirs': [], 'file_count':0, 'dir_count':0}
                        if stack not in archives[top]['stacks']:
                            archives[top]['stacks'][stack] = {'mismatched_files': [], 'mismatched_dirs': []}
                        archives[top]['stacks'][stack]['mismatched_dirs'].append({'path': p, 'mode': format_mode(mode)})
                except Exception:
                    continue
            for f in files:
                total_files += 1
                p = os.path.join(root, f)
                try:
                    mode = os.stat(p).st_mode & 0o777
                    if mode != file_mode:
                        rel = os.path.relpath(p, base)
                        parts = rel.split(os.sep) if rel and not rel.startswith('..') else []
                        top = parts[0] if len(parts) >= 1 else ''
                        stack = parts[1] if len(parts) >= 2 else '<root>'
                        if top not in archives:
                            archives[top] = {'path': os.path.join(base, top), 'stacks': {}, 'mismatched_files': [], 'mismatched_dirs': [], 'file_count':0, 'dir_count':0}
                        if stack not in archives[top]['stacks']:
                            archives[top]['stacks'][stack] = {'mismatched_files': [], 'mismatched_dirs': []}
                        archives[top]['stacks'][stack]['mismatched_files'].append({'path': p, 'mode': format_mode(mode)})
                except Exception:
                    continue

        # Prepare response: convert archives dict to list with counts and limited samples per stack
        archive_list = []
        for name, data in archives.items():
            stacks_out = []
            for sname, sdata in data.get('stacks', {}).items():
                stacks_out.append({
                    'name': sname,
                    'mismatched_file_count': len(sdata.get('mismatched_files', [])),
                    'mismatched_dir_count': len(sdata.get('mismatched_dirs', [])),
                    'sample_files': sdata.get('mismatched_files', [])[:5],
                    'sample_dirs': sdata.get('mismatched_dirs', [])[:5]
                })
            archive_list.append({
                'name': name,
                'path': data['path'],
                'mismatched_file_count': sum(s['mismatched_file_count'] for s in stacks_out),
                'mismatched_dir_count': sum(s['mismatched_dir_count'] for s in stacks_out),
                'stacks': stacks_out
            })

        # Flatten stacks into a top-level list for simpler UI consumption
        stacks_list = []
        for a in archive_list:
            for s in a.get('stacks', []):
                stacks_list.append({
                    'archive_name': a['name'],
                    'archive_path': a['path'],
                    'stack_name': s['name'],
                    'mismatched_file_count': s['mismatched_file_count'],
                    'mismatched_dir_count': s['mismatched_dir_count'],
                    'sample_files': s['sample_files'],
                    'sample_dirs': s['sample_dirs']
                })

        return jsonify({
            'status': 'ok',
            'total_files': total_files,
            'total_dirs': total_dirs,
            'archives': archive_list,
            'stacks': stacks_list,
            'mismatched_file_count': sum(a['mismatched_file_count'] for a in archive_list),
            'mismatched_dir_count': sum(a['mismatched_dir_count'] for a in archive_list)
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@bp.route('/downloads')
@login_required
def manage_downloads():
    """Download tokens management page."""
    return render_template('settings_downloads.html')

"""
Settings routes.
"""
import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from app.auth import login_required, get_current_user
from app.db import get_db
from app.scheduler import reload_schedules
from app.notifications import send_test_notification


bp = Blueprint('settings', __name__, url_prefix='/settings')


def validate_apprise_urls(urls_text):
    """
    Validate and clean Apprise URLs:
    - Remove mailto/mailtos URLs (email should be configured via SMTP environment)
    - Remove duplicates
    - Return tuple: (cleaned_urls_string, blocked_count, duplicate_count)
    """
    if not urls_text:
        return '', 0, 0
    
    valid_urls = []
    seen_urls = set()
    blocked_protocols = ['mailto://', 'mailtos://']
    blocked_count = 0
    duplicate_count = 0
    
    for url in urls_text.strip().split('\n'):
        url = url.strip()
        if not url:
            continue
        
        # Check if URL uses blocked protocols
        is_blocked = any(url.lower().startswith(proto) for proto in blocked_protocols)
        if is_blocked:
            blocked_count += 1
            continue
        
        # Check for duplicates (case-insensitive)
        url_lower = url.lower()
        if url_lower in seen_urls:
            duplicate_count += 1
            continue
        
        seen_urls.add(url_lower)
        valid_urls.append(url)
    
    return '\n'.join(valid_urls), blocked_count, duplicate_count


@bp.route('/', methods=['GET', 'POST'])
@login_required
def manage_settings():
    """Settings page."""
    if request.method == 'POST':
        try:
            # Update settings
            base_url = request.form.get('base_url', 'http://localhost:8080')
            apprise_urls_raw = request.form.get('apprise_urls', '')
            notification_subject_tag = request.form.get('notification_subject_tag', '')
            notify_success = request.form.get('notify_success') == 'on'
            notify_error = request.form.get('notify_error') == 'on'
            notify_html_format = request.form.get('notify_html_format') == 'on'
            notify_report_verbosity = request.form.get('notify_report_verbosity', 'full')
            notify_attach_log = request.form.get('notify_attach_log') == 'on'
            notify_attach_log_on_failure = request.form.get('notify_attach_log_on_failure') == 'on'
            apply_permissions = request.form.get('apply_permissions') == 'on'
            
            # Validate and clean Apprise URLs
            apprise_urls, blocked_count, duplicate_count = validate_apprise_urls(apprise_urls_raw)
            
            # Inform user about blocked/removed URLs
            if blocked_count > 0:
                flash(f'⚠️ {blocked_count} mailto URL(s) removed. Please use SMTP environment variables for email notifications.', 'warning')
            if duplicate_count > 0:
                flash(f'ℹ️ {duplicate_count} duplicate URL(s) removed.', 'info')
            
            maintenance_mode = request.form.get('maintenance_mode') == 'on'
            max_token_downloads = request.form.get('max_token_downloads', '3')
            
            # Cleanup settings
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
                    return redirect(url_for('settings.manage_settings'))

            with get_db() as conn:
                cur = conn.cursor()
                settings_to_update = [
                    ('base_url', base_url),
                    ('apprise_urls', apprise_urls),
                    ('notification_subject_tag', notification_subject_tag),
                    ('notify_on_success', 'true' if notify_success else 'false'),
                    ('notify_on_error', 'true' if notify_error else 'false'),
                    ('notify_html_format', 'true' if notify_html_format else 'false'),
                    ('maintenance_mode', 'true' if maintenance_mode else 'false'),
                    ('max_token_downloads', max_token_downloads),
                    ('cleanup_enabled', 'true' if cleanup_enabled else 'false'),
                    ('cleanup_cron', cleanup_cron),
                    ('cleanup_log_retention_days', cleanup_log_retention_days),
                    ('cleanup_dry_run', 'true' if cleanup_dry_run else 'false'),
                    ('notify_on_cleanup', 'true' if notify_cleanup else 'false'),
                    ('notify_report_verbosity', notify_report_verbosity),
                    ('notify_attach_log', 'true' if notify_attach_log else 'false'),
                    ('notify_attach_log_on_failure', 'true' if notify_attach_log_on_failure else 'false'),
                    ('apply_permissions', 'true' if apply_permissions else 'false'),
                ]
                
                for key, value in settings_to_update:
                    cur.execute("""
                        INSERT INTO settings (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP;
                    """, (key, value, value))
                
                conn.commit()
            
            # Reload scheduler if maintenance mode changed
            reload_schedules()
            try:
                from app.scheduler import publish_reload_signal
                publish_reload_signal()
            except Exception:
                pass
            
            # Reschedule cleanup task if settings changed
            from app.scheduler import schedule_cleanup_task
            schedule_cleanup_task()
            
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
    
    # Check if SMTP is configured via environment variables
    smtp_configured = bool(os.environ.get('SMTP_SERVER') and os.environ.get('SMTP_USER'))
    
    return render_template(
        'settings.html',
        settings=settings_dict,
        current_user=get_current_user(),
        smtp_configured=smtp_configured
    )


@bp.route('/test-notification', methods=['POST'])
@login_required
def test_notification():
    """Send a test notification."""
    try:
        send_test_notification()
        return jsonify({'success': True, 'message': 'Test notification sent successfully!'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to send notification: {str(e)}'}), 500


@bp.route('/fix-permissions', methods=['POST'])
@login_required
def fix_permissions():
    """Start a background task to apply configured permissions to existing archives."""
    try:
        import threading
        from app.utils import get_archives_path, apply_permissions_recursive, get_logger
        logger = get_logger(__name__)

        def _run():
            try:
                base = get_archives_path()
                logger.info("[FixPerm] Starting permission fix on %s", base)
                res = apply_permissions_recursive(base)
                logger.info("[FixPerm] Completed: %s", res)
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
                        top = rel.split(os.sep)[0] if rel and not rel.startswith('..') else rel
                        if top not in archives:
                            archives[top] = {'path': os.path.join(base, top), 'mismatched_files': [], 'mismatched_dirs': [], 'file_count':0, 'dir_count':0}
                        archives[top]['mismatched_dirs'].append({'path': p, 'mode': oct(mode)})
                except Exception:
                    continue
            for f in files:
                total_files += 1
                p = os.path.join(root, f)
                try:
                    mode = os.stat(p).st_mode & 0o777
                    if mode != file_mode:
                        rel = os.path.relpath(p, base)
                        top = rel.split(os.sep)[0] if rel and not rel.startswith('..') else rel
                        if top not in archives:
                            archives[top] = {'path': os.path.join(base, top), 'mismatched_files': [], 'mismatched_dirs': [], 'file_count':0, 'dir_count':0}
                        archives[top]['mismatched_files'].append({'path': p, 'mode': oct(mode)})
                except Exception:
                    continue

        # Prepare response: convert archives dict to list with counts and limited samples
        archive_list = []
        for name, data in archives.items():
            archive_list.append({
                'name': name,
                'path': data['path'],
                'mismatched_file_count': len(data['mismatched_files']),
                'mismatched_dir_count': len(data['mismatched_dirs']),
                'sample_files': data['mismatched_files'][:10],
                'sample_dirs': data['mismatched_dirs'][:10]
            })

        return jsonify({
            'status': 'ok',
            'total_files': total_files,
            'total_dirs': total_dirs,
            'archives': archive_list,
            'mismatched_file_count': sum(a['mismatched_file_count'] for a in archive_list),
            'mismatched_dir_count': sum(a['mismatched_dir_count'] for a in archive_list)
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

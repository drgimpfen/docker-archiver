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
            cleanup_time = request.form.get('cleanup_time', '02:30')
            cleanup_log_retention_days = request.form.get('cleanup_log_retention_days', '90')
            cleanup_dry_run = request.form.get('cleanup_dry_run') == 'on'
            notify_cleanup = request.form.get('notify_cleanup') == 'on'
            
            # Validate cleanup time format
            if cleanup_enabled:
                try:
                    hour, minute = map(int, cleanup_time.split(':'))
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        raise ValueError
                except (ValueError, AttributeError):
                    flash('Invalid cleanup time format. Please use HH:MM format (e.g., 02:30).', 'danger')
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
                    ('cleanup_time', cleanup_time),
                    ('cleanup_log_retention_days', cleanup_log_retention_days),
                    ('cleanup_dry_run', 'true' if cleanup_dry_run else 'false'),
                    ('notify_on_cleanup', 'true' if notify_cleanup else 'false'),
                    ('notify_report_verbosity', notify_report_verbosity),
                    ('notify_attach_log', 'true' if notify_attach_log else 'false'),
                    ('notify_attach_log_on_failure', 'true' if notify_attach_log_on_failure else 'false'),
                ]
                
                for key, value in settings_to_update:
                    cur.execute("""
                        INSERT INTO settings (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP;
                    """, (key, value, value))
                
                conn.commit()
            
            # Reload scheduler if maintenance mode changed
            reload_schedules()
            
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

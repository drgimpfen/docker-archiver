"""
Settings routes.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from app.auth import login_required, get_current_user
from app.db import get_db
from app.scheduler import reload_schedules


bp = Blueprint('settings', __name__, url_prefix='/settings')


@bp.route('/', methods=['GET', 'POST'])
@login_required
def manage_settings():
    """Settings page."""
    if request.method == 'POST':
        try:
            # Update settings
            base_url = request.form.get('base_url', 'http://localhost:8080')
            apprise_urls = request.form.get('apprise_urls', '')
            notify_success = request.form.get('notify_success') == 'on'
            notify_error = request.form.get('notify_error') == 'on'
            maintenance_mode = request.form.get('maintenance_mode') == 'on'
            
            with get_db() as conn:
                cur = conn.cursor()
                settings_to_update = [
                    ('base_url', base_url),
                    ('apprise_urls', apprise_urls),
                    ('notify_on_success', 'true' if notify_success else 'false'),
                    ('notify_on_error', 'true' if notify_error else 'false'),
                    ('maintenance_mode', 'true' if maintenance_mode else 'false'),
                ]
                
                for key, value in settings_to_update:
                    cur.execute("""
                        INSERT INTO settings (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP;
                    """, (key, value, value))
                
                conn.commit()
            
            # Reload scheduler if maintenance mode changed
            reload_schedules()
            
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
    
    return render_template(
        'settings.html',
        settings=settings_dict,
        current_user=get_current_user()
    )

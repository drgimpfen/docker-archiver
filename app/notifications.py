"""
Notification system using Apprise.
"""
import os
from app.db import get_db


def get_setting(key, default=''):
    """Get a setting value from database."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = %s;", (key,))
            result = cur.fetchone()
            return result['value'] if result else default
    except Exception:
        return default


def get_user_emails():
    """Get all user email addresses that are configured."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT email FROM users WHERE email IS NOT NULL AND email != '';")
            results = cur.fetchall()
            return [row['email'] for row in results]
    except Exception:
        return []


def get_subject_with_tag(subject):
    """Add optional subject tag prefix to notification subject."""
    tag = get_setting('notification_subject_tag', '').strip()
    if tag:
        return f"{tag} {subject}"
    return subject


def get_notification_format():
    """Get notification format (html or text) from settings."""
    import apprise
    html_enabled = get_setting('notify_html_format', 'true').lower() == 'true'
    return apprise.NotifyFormat.HTML if html_enabled else apprise.NotifyFormat.TEXT


def strip_html_tags(html_text):
    """Convert HTML to plain text by removing tags and converting entities."""
    import re
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', html_text)
    # Convert common HTML entities
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&amp;', '&')
    # Clean up multiple newlines
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()


def get_apprise_instance():
    """
    Create and configure Apprise instance with URLs from settings and environment.
    Automatically includes user emails if SMTP is configured via environment.
    """
    import apprise
    
    apobj = apprise.Apprise()
    
    # Add URLs from settings
    apprise_urls = get_setting('apprise_urls', '')
    for url in apprise_urls.strip().split('\n'):
        url = url.strip()
        if url:
            apobj.add(url)
    
    # Add SMTP/Email if configured via environment variables
    smtp_server = os.environ.get('SMTP_SERVER')
    smtp_user = os.environ.get('SMTP_USER')
    smtp_password = os.environ.get('SMTP_PASSWORD')
    smtp_port = os.environ.get('SMTP_PORT', '587')
    smtp_from = os.environ.get('SMTP_FROM')
    
    if smtp_server and smtp_user and smtp_password and smtp_from:
        # Get user emails
        user_emails = get_user_emails()
        
        for email in user_emails:
            # Build mailto URL for Apprise
            # Format: mailtos://user:pass@server:port/?from=sender&to=recipient
            mailto_url = f"mailtos://{smtp_user}:{smtp_password}@{smtp_server}:{smtp_port}/?from={smtp_from}&to={email}"
            apobj.add(mailto_url)
    
    return apobj


def should_notify(event_type):
    """Check if notifications are enabled for this event type."""
    key = f"notify_on_{event_type}"
    value = get_setting(key, 'false')
    return value.lower() == 'true'


def send_archive_notification(archive_config, job_id, stack_metrics, duration, total_size):
    """Send notification for archive job completion."""
    if not should_notify('success'):
        return
    
    try:
        base_url = get_setting('base_url', 'http://localhost:8080')
        
        # Create Apprise instance with all configured URLs and emails
        apobj = get_apprise_instance()
        
        if not apobj:
            return
        
        # Build notification message
        archive_name = archive_config['name']
        stack_count = len(stack_metrics)
        success_count = sum(1 for m in stack_metrics if m['status'] == 'success')
        failed_count = stack_count - success_count
        
        size_mb = total_size / (1024 * 1024)
        size_gb = total_size / (1024 * 1024 * 1024)
        size_str = f"{size_gb:.2f}GB" if size_gb >= 1 else f"{size_mb:.1f}MB"
        
        duration_min = duration // 60
        duration_sec = duration % 60
        duration_str = f"{duration_min}m {duration_sec}s" if duration_min > 0 else f"{duration_sec}s"
        
        status_emoji = "✅" if failed_count == 0 else "⚠️"
        
        title = get_subject_with_tag(f"{status_emoji} Archive Complete: {archive_name}")
        
        # Check if any stacks have named volumes
        stacks_with_volumes = [m for m in stack_metrics if m.get('named_volumes')]
        
        body = f"""<h2>Archive job completed for: {archive_name}</h2>
<p>
<strong>Stacks:</strong> {success_count}/{stack_count} successful<br>
<strong>Total size:</strong> {size_str}<br>
<strong>Duration:</strong> {duration_str}
</p>
<h3>Stacks processed:</h3>
<ul>
"""
        
        for metric in stack_metrics:
            stack_name = metric['stack_name']
            status_icon = "✓" if metric['status'] == 'success' else "✗"
            stack_size_mb = metric['archive_size_bytes'] / (1024 * 1024)
            
            # Add volume warning if present
            volume_warning = ""
            if metric.get('named_volumes'):
                volumes = metric['named_volumes']
                volume_warning = f" <span style='color: orange;'>⚠️ {len(volumes)} named volume(s) not backed up</span>"
            
            body += f"<li>{status_icon} {stack_name} ({stack_size_mb:.1f}MB){volume_warning}</li>\n"
        
        body += f"""</ul>"""
        
        # Add global warning about named volumes
        if stacks_with_volumes:
            body += f"""
<hr>
<p style='color: orange;'><strong>⚠️ Named Volumes Warning</strong></p>
<p>The following stacks use named volumes that are <strong>NOT included</strong> in the backup archives:</p>
<ul>
"""
            for metric in stacks_with_volumes:
                volumes = metric['named_volumes']
                body += f"<li><strong>{metric['stack_name']}:</strong> {', '.join(volumes)}</li>\n"
            
            body += """</ul>
<p>Named volumes require separate backup using tools like <code>docker volume backup</code> or similar.</p>
<hr>
"""
        
        body += f"""<p><a href="{base_url}/history?job={job_id}">View details</a></p>
<hr>
<p><small>Docker Archiver: <a href="{base_url}">{base_url}</a></small></p>"""
        
        # Get format preference
        body_format = get_notification_format()
        
        # Convert to plain text if needed
        import apprise
        if body_format == apprise.NotifyFormat.TEXT:
            body = strip_html_tags(body)
        
        # Send notification
        apobj.notify(
            body=body,
            title=title,
            body_format=body_format
        )
        
    except Exception as e:
        print(f"[WARNING] Failed to send notification: {e}")


def send_retention_notification(archive_name, deleted_count, reclaimed_bytes):
    """Send notification for retention job completion."""
    if not should_notify('success'):
        return
    
    try:
        # Create Apprise instance with all configured URLs and emails
        apobj = get_apprise_instance()
        
        if not apobj:
            return
        
        # Build message
        reclaimed_gb = reclaimed_bytes / (1024 * 1024 * 1024)
        reclaimed_mb = reclaimed_bytes / (1024 * 1024)
        size_str = f"{reclaimed_gb:.2f}GB" if reclaimed_gb >= 1 else f"{reclaimed_mb:.1f}MB"
        
        base_url = get_setting('base_url', 'http://localhost:8080')
        
        title = get_subject_with_tag(f"🗑️ Retention Cleanup: {archive_name}")
        body = f"""<h2>Retention cleanup completed for: {archive_name}</h2>
<p>
<strong>Files deleted:</strong> {deleted_count}<br>
<strong>Space freed:</strong> {size_str}
</p>
<hr>
<p><small>Docker Archiver: <a href="{base_url}">{base_url}</a></small></p>"""
        
        # Get format preference
        body_format = get_notification_format()
        
        # Convert to plain text if needed
        import apprise
        if body_format == apprise.NotifyFormat.TEXT:
            body = strip_html_tags(body)
        
        # Send notification
        apobj.notify(
            body=body,
            title=title,
            body_format=body_format
        )
        
    except Exception as e:
        print(f"[WARNING] Failed to send notification: {e}")


def send_error_notification(archive_name, error_message):
    """Send notification for job failure."""
    if not should_notify('error'):
        return
    
    try:
        # Create Apprise instance with all configured URLs and emails
        apobj = get_apprise_instance()
        
        if not apobj:
            return
        
        base_url = get_setting('base_url', 'http://localhost:8080')
        
        title = get_subject_with_tag(f"❌ Archive Failed: {archive_name}")
        body = f"""<h2>Archive job failed for: {archive_name}</h2>
<p>
<strong>Error:</strong><br>
<code>{error_message}</code>
</p>
<hr>
<p><small>Docker Archiver: <a href="{base_url}">{base_url}</a></small></p>"""
        
        # Get format preference
        body_format = get_notification_format()
        
        # Convert to plain text if needed
        import apprise
        if body_format == apprise.NotifyFormat.TEXT:
            body = strip_html_tags(body)
        
        # Send notification
        apobj.notify(
            body=body,
            title=title,
            body_format=body_format
        )
        
    except Exception as e:
        print(f"[WARNING] Failed to send notification: {e}")


def send_test_notification():
    """Send a test notification to verify configuration."""
    try:
        # Create Apprise instance with all configured URLs and emails
        apobj = get_apprise_instance()
        
        if not apobj:
            raise Exception("No notification services configured")
        
        base_url = get_setting('base_url', 'http://localhost:8080')
        
        title = get_subject_with_tag("🔔 Docker Archiver - Test Notification")
        body = f"""<h2>Test Notification from Docker Archiver</h2>
<p>If you received this message, your notification configuration is working correctly!</p>
<h3>Notification services configured:</h3>
<ul>
<li>Apprise URLs from settings</li>
<li>User email addresses (if SMTP is configured)</li>
</ul>
<hr>
<p><small>Docker Archiver: <a href="{base_url}">{base_url}</a></small></p>"""
        
        # Get format preference
        body_format = get_notification_format()
        
        # Convert to plain text if needed
        import apprise
        if body_format == apprise.NotifyFormat.TEXT:
            body = strip_html_tags(body)
        
        # Send notification
        apobj.notify(
            body=body,
            title=title,
            body_format=body_format
        )
        
    except Exception as e:
        raise Exception(f"Failed to send test notification: {e}")

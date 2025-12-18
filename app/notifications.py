"""
Notification system using Apprise.
"""
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
        import apprise
        
        apprise_urls = get_setting('apprise_urls', '')
        if not apprise_urls:
            return
        
        base_url = get_setting('base_url', 'http://localhost:8080')
        
        # Create Apprise instance
        apobj = apprise.Apprise()
        
        # Add URLs
        for url in apprise_urls.strip().split('\n'):
            url = url.strip()
            if url:
                apobj.add(url)
        
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
        
        title = f"{status_emoji} Archive Complete: {archive_name}"
        
        body = f"""Archive job completed for: {archive_name}

Stacks: {success_count}/{stack_count} successful
Total size: {size_str}
Duration: {duration_str}

Stacks processed:
"""
        
        for metric in stack_metrics:
            stack_name = metric['stack_name']
            status_icon = "✓" if metric['status'] == 'success' else "✗"
            stack_size_mb = metric['archive_size_bytes'] / (1024 * 1024)
            body += f"  {status_icon} {stack_name} ({stack_size_mb:.1f}MB)\n"
        
        body += f"\nView details: {base_url}/history?job={job_id}"
        
        # Send notification
        apobj.notify(
            body=body,
            title=title,
        )
        
    except Exception as e:
        print(f"[WARNING] Failed to send notification: {e}")


def send_retention_notification(archive_name, deleted_count, reclaimed_bytes):
    """Send notification for retention job completion."""
    if not should_notify('success'):
        return
    
    try:
        import apprise
        
        apprise_urls = get_setting('apprise_urls', '')
        if not apprise_urls:
            return
        
        # Create Apprise instance
        apobj = apprise.Apprise()
        
        # Add URLs
        for url in apprise_urls.strip().split('\n'):
            url = url.strip()
            if url:
                apobj.add(url)
        
        # Build message
        reclaimed_gb = reclaimed_bytes / (1024 * 1024 * 1024)
        reclaimed_mb = reclaimed_bytes / (1024 * 1024)
        size_str = f"{reclaimed_gb:.2f}GB" if reclaimed_gb >= 1 else f"{reclaimed_mb:.1f}MB"
        
        title = f"🗑️ Retention Cleanup: {archive_name}"
        body = f"""Retention cleanup completed for: {archive_name}

Files deleted: {deleted_count}
Space freed: {size_str}
"""
        
        # Send notification
        apobj.notify(
            body=body,
            title=title,
        )
        
    except Exception as e:
        print(f"[WARNING] Failed to send notification: {e}")


def send_error_notification(archive_name, error_message):
    """Send notification for job failure."""
    if not should_notify('error'):
        return
    
    try:
        import apprise
        
        apprise_urls = get_setting('apprise_urls', '')
        if not apprise_urls:
            return
        
        # Create Apprise instance
        apobj = apprise.Apprise()
        
        # Add URLs
        for url in apprise_urls.strip().split('\n'):
            url = url.strip()
            if url:
                apobj.add(url)
        
        title = f"❌ Archive Failed: {archive_name}"
        body = f"""Archive job failed for: {archive_name}

Error: {error_message}
"""
        
        # Send notification
        apobj.notify(
            body=body,
            title=title,
        )
        
    except Exception as e:
        print(f"[WARNING] Failed to send notification: {e}")

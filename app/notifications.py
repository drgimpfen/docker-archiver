"""
Notification system using Apprise.
"""
import os
from app.db import get_db
from app.utils import format_bytes, format_duration, get_disk_usage, get_archives_path


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
        
        # Use shared formatting for human-readable sizes so all sizes are consistent
        size_str = format_bytes(total_size)
        
        duration_min = duration // 60
        duration_sec = duration % 60
        duration_str = f"{duration_min}m {duration_sec}s" if duration_min > 0 else f"{duration_sec}s"
        
        status_emoji = "✅" if failed_count == 0 else "⚠️"
        
        title = get_subject_with_tag(f"{status_emoji} Archive Complete: {archive_name}")
        
        # Check if any stacks have named volumes
        stacks_with_volumes = [m for m in stack_metrics if m.get('named_volumes')]
        
        # Header & visual styles
        css = """
        <style>
        .da-table { width: 100%; border-collapse: collapse; font-family: monospace; }
        .da-table th, .da-table td { padding: 6px 8px; border-bottom: 1px solid #eee; text-align: left; }
        .da-badge-ok { color: #155724; background: #d4edda; padding: 2px 6px; border-radius: 4px; }
        .da-badge-fail { color: #721c24; background: #f8d7da; padding: 2px 6px; border-radius: 4px; }
        .da-small { font-size: 90%; color: #666; }
        </style>
        """

        body = f"""
{css}
<div style='font-family: Arial, Helvetica, sans-serif; max-width:800px; margin:0; text-align:left; color:#222;'>
  <h2 style='margin-bottom:6px;'>{status_emoji} Archive job completed: <strong>{archive_name}</strong></h2>
  <p class='da-small'><strong>Stacks:</strong> {success_count}/{stack_count} successful &nbsp;|&nbsp; <strong>Total size:</strong> {size_str} &nbsp;|&nbsp; <strong>Duration:</strong> {duration_str}</p>
"""

        # Created archives table (alphabetical by filename)
        created_archives = []
        for m in stack_metrics:
            path = m.get('archive_path')
            size = m.get('archive_size_bytes') or 0
            if path:
                created_archives.append({'path': str(path), 'size': size})

        if created_archives:
            created_archives.sort(key=lambda x: x['path'].split('/')[-1].lower())
            body += """
  <h3>SUMMARY OF CREATED ARCHIVES</h3>
  <table class='da-table'>
    <thead><tr><th style='width:110px;'>Size</th><th>Filename</th></tr></thead>
    <tbody>
"""
            for a in created_archives:
                body += f"    <tr><td>{format_bytes(a['size'])}</td><td><code>{a['path']}</code></td></tr>\n"
            body += f"  </tbody></table>\n  <p class='da-small'><strong>Total:</strong> {format_bytes(total_size)}</p>\n"
        else:
            body += "  <p><em>No archives were created.</em></p>\n"

        # Disk usage
        try:
            disk = get_disk_usage()
            if disk and disk['total']:
                body += """
  <h3>DISK USAGE (on /archives)</h3>
  <p class='da-small'>
"""
                body += f"    Total: <strong>{format_bytes(disk['total'])}</strong> &nbsp; Used: <strong>{format_bytes(disk['used'])}</strong> ({disk['percent']:.0f}% used)\n"
                try:
                    import os
                    total_archives_size = 0
                    for root, dirs, files in os.walk(get_archives_path()):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            try:
                                total_archives_size += os.path.getsize(fp)
                            except Exception:
                                continue
                    body += f"  <br>Backup Content Size (/archives): <strong>{format_bytes(total_archives_size)}</strong>\n"
                except Exception:
                    pass
                body += "  </p>\n"
        except Exception:
            pass

        # Retention summary
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT reclaimed_bytes, log FROM jobs WHERE id = %s;", (job_id,))
                job_row = cur.fetchone()
                reclaimed = job_row.get('reclaimed_bytes') if job_row else None
                job_log = job_row.get('log') if job_row else ''

            body += """
  <h3>RETENTION SUMMARY</h3>
  <p class='da-small'>
"""
            if reclaimed is None:
                body += "    No retention information available.\n"
            elif reclaimed == 0:
                import re
                m = re.search(r"Local cleanup finished\.[^\n]*", job_log or '')
                if m:
                    body += f"    {m.group(0)}\n"
                else:
                    body += "    No files older than configured retention were deleted.\n"
            else:
                body += f"    Freed space: <strong>{format_bytes(reclaimed)}</strong>\n"
            body += "  </p>\n"
        except Exception:
            pass

        # Stacks processed table
        body += """
  <h3>STACKS PROCESSED</h3>
  <table class='da-table'>
    <thead><tr><th>Stack</th><th>Status</th><th>Size</th><th>Archive</th></tr></thead>
    <tbody>
"""
        for metric in stack_metrics:
            stack_name = metric['stack_name']
            status_ok = metric['status'] == 'success'
            status_html = f"<span class='{'da-badge-ok' if status_ok else 'da-badge-fail'}'>{'✓' if status_ok else '✗'}</span>"
            stack_size_str = format_bytes(metric.get('archive_size_bytes') or 0)
            archive_path = metric.get('archive_path') or ''
            body += f"    <tr><td>{stack_name}</td><td>{status_html}</td><td>{stack_size_str}</td><td><code>{archive_path or 'N/A'}</code></td></tr>\n"
        body += "  </tbody></table>\n"

        # Named volumes warning block
        if stacks_with_volumes:
            body += """
  <hr>
  <h4 style='color:orange;'>⚠️ Named Volumes Warning</h4>
  <p class='da-small'>Named volumes are NOT included in the backup archives. Consider backing them up separately.</p>
  <ul>
"""
            for metric in stacks_with_volumes:
                volumes = metric['named_volumes']
                body += f"    <li><strong>{metric['stack_name']}:</strong> {', '.join(volumes)}</li>\n"
            body += "  </ul>\n"

        # Full log (always expanded unless log will be attached)
        try:
            if job_row and job_row.get('log'):
                # Determine if the log will be attached instead of inlined
                attach_log_setting = get_setting('notify_attach_log', 'false').lower() == 'true'
                attach_on_failure_setting = get_setting('notify_attach_log_on_failure', 'false').lower() == 'true'
                should_attach_log = attach_log_setting or (attach_on_failure_setting and failed_count > 0)

                if not should_attach_log:
                    body += """
  <hr>
  <h3>Full job log</h3>
  <pre style='background:#f7f7f7;padding:10px;border-radius:6px;white-space:pre-wrap;'>\n"""
                    body += (job_row.get('log') or '') + "\n"
                    body += "  </pre>\n"
                else:
                    # If log will be attached, omit the inline log to avoid duplication
                    body += "\n"
        except Exception:
            pass

        # Footer
        body += f"<p class='da-small'><a href=\"{base_url}/history?job={job_id}\">View details</a> &nbsp;|&nbsp; Docker Archiver: <a href=\"{base_url}\">{base_url}</a></p>"
        
        # Get format preference and user settings
        body_format = get_notification_format()
        verbosity = get_setting('notify_report_verbosity', 'full')
        attach_log_setting = get_setting('notify_attach_log', 'false').lower() == 'true'
        attach_on_failure_setting = get_setting('notify_attach_log_on_failure', 'false').lower() == 'true'

        # If user chose short verbosity, construct compact message
        if verbosity == 'short':
            short_body = f"<h2>{status_emoji} Archive: <strong>{archive_name}</strong></h2>\n"
            short_body += f"<p class='da-small'><strong>Stacks:</strong> {success_count}/{stack_count} successful &nbsp;|&nbsp; <strong>Total:</strong> {size_str} &nbsp;|&nbsp; <strong>Duration:</strong> {format_duration(duration)}</p>\n"
            # concise per-stack list
            short_body += "<p>"
            short_body += ", ".join([f"{m['stack_name']} ({format_bytes(m.get('archive_size_bytes') or 0)})" for m in stack_metrics])
            short_body += "</p>\n"
            short_body += f"<p><a href=\"{base_url}/history?job={job_id}\">View details</a></p>\n"
            body_to_send = short_body
        else:
            body_to_send = body

        # Convert to plain text if needed
        import apprise
        if body_format == apprise.NotifyFormat.TEXT:
            send_body = strip_html_tags(body_to_send)
        else:
            send_body = body_to_send

        # Optionally attach full job log as a file instead of inlining it
        attach_path = None
        try:
            # Decide whether to attach the log based on settings and job outcome
            should_attach = False
            if attach_log_setting:
                should_attach = True
            elif attach_on_failure_setting and failed_count > 0:
                should_attach = True

            if should_attach:
                # Fetch job log from DB (best-effort)
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT log FROM jobs WHERE id = %s;", (job_id,))
                    row = cur.fetchone()
                    job_log = row.get('log') if row else ''

                if job_log:
                    import tempfile, os
                    tf = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log', prefix=f'job_{job_id}_')
                    try:
                        tf.write(job_log)
                        tf.flush()
                        attach_path = tf.name
                    finally:
                        tf.close()
        except Exception as e:
            logger.exception("Failed to prepare log attachment: %s", e)

        # Send notification (with optional attachment)
        try:
            if attach_path:
                apobj.notify(
                    body=send_body,
                    title=title,
                    body_format=body_format,
                    attach=attach_path
                )
            else:
                apobj.notify(
                    body=send_body,
                    title=title,
                    body_format=body_format
                )
        finally:
            # Cleanup temporary file if used
            try:
                import os
                if attach_path and os.path.exists(attach_path):
                    os.unlink(attach_path)
            except Exception:
                pass
        
    except Exception as e:
        logger.exception("Failed to send notification: %s", e)


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
        logger.exception("Failed to send notification: %s", e)


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
        logger.exception("Failed to send notification: %s", e)


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

"""
Notification system using Apprise.
"""
import os
from app.db import get_db
from app.utils import format_bytes, format_duration, get_disk_usage, get_archives_path, get_logger, setup_logging

# Configuration constants (can be tweaked or moved to settings later)
NOTIFY_MAX_DISCORD_MSG = 1800
NOTIFY_EMBED_DESC_MAX = 4000
NOTIFY_EMBED_BATCH_SIZE = 10
NOTIFY_MAX_OTHER_MSG = 1500

# Configure logging
setup_logging()
logger = get_logger(__name__)


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
    """Convert HTML to plain text by removing tags and converting entities.

    Also removes <style> and <script> blocks to avoid leaving CSS/JS content behind.
    """
    import re
    if not html_text:
        return ''
    # Remove style/script blocks entirely
    text = re.sub(r'<(script|style)[\s\S]*?>[\s\S]*?<\/\1>', '', html_text, flags=re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Convert common HTML entities
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&amp;', '&')
    # Normalize whitespace and clean up multiple newlines
    text = re.sub(r'\r\n?', '\n', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


# Helper: build a compact plain-text summary for non-email services
def build_compact_text(archive_name, stack_metrics, created_archives, total_size, size_str, duration_str, stacks_with_volumes, reclaimed, base_url):
    lines = []
    lines.append(f"{archive_name} completed")
    lines.append(f"Stacks: {sum(1 for m in stack_metrics if m.get('status') == 'success')}/{len(stack_metrics)} successful  |  Total size: {size_str}  |  Duration: {duration_str}")
    lines.append("")

    if created_archives:
        lines.append("SUMMARY OF CREATED ARCHIVES")
        lines.append("")
        for a in created_archives:
            lines.append(f"{format_bytes(a['size'])} {a['path']}")
        lines.append("")
        lines.append(f"Total: {format_bytes(total_size)}")
        lines.append("")

    try:
        disk = get_disk_usage()
        if disk and disk['total']:
            lines.append("DISK USAGE (on /archives)")
            lines.append("")
            lines.append(f"Total: {format_bytes(disk['total'])}   Used: {format_bytes(disk['used'])} ({disk['percent']:.0f}% used)")
            try:
                total_archives_size = 0
                for root, dirs, files in __import__('os').walk(get_archives_path()):
                    for fn in files:
                        fp = __import__('os').path.join(root, fn)
                        try:
                            total_archives_size += __import__('os').path.getsize(fp)
                        except Exception:
                            continue
                lines.append(f"Backup Content Size (/archives): {format_bytes(total_archives_size)}")
            except Exception:
                pass
            lines.append("")
    except Exception:
        pass

    # Retention
    if reclaimed is None:
        lines.append("RETENTION SUMMARY")
        lines.append("")
        lines.append("No retention information available.")
        lines.append("")
    elif reclaimed == 0:
        lines.append("RETENTION SUMMARY")
        lines.append("")
        lines.append("No archives older than configured retention were deleted.")
        lines.append("")
    else:
        lines.append("RETENTION SUMMARY")
        lines.append("")
        lines.append(f"Freed space: {format_bytes(reclaimed)}")
        lines.append("")

    if stack_metrics:
        lines.append("STACKS PROCESSED")
        lines.append("")
        for metric in stack_metrics:
            ok = '✓' if metric.get('status') == 'success' else '✗'
            st_size = format_bytes(metric.get('archive_size_bytes') or 0)
            archive_p = metric.get('archive_path') or 'N/A'
            lines.append(f"{metric['stack_name']} {ok} {st_size} {archive_p}")
        lines.append("")

    if stacks_with_volumes:
        lines.append("⚠️ Named Volumes Warning")
        lines.append("Named volumes are NOT included in the backup archives. Consider backing them up separately.")
        lines.append("")
        for metric in stacks_with_volumes:
            volumes = metric.get('named_volumes') or []
            lines.append(f"{metric['stack_name']}: {', '.join(volumes)}")
        lines.append("")

    lines.append(f"View details: {base_url}/history?job=")

    compact_text = "\n".join(lines)

    # Truncate to safe size for chat services (Discord message limit ~2000 chars)
    if len(compact_text) > NOTIFY_MAX_DISCORD_MSG:
        compact_text = compact_text[:NOTIFY_MAX_DISCORD_MSG] + "\n\n[Message truncated; full log attached]"

    return compact_text, lines


# Helper: split a long section into chunks respecting max_len
def split_section_by_length(text, max_len):
    if not text:
        return ['']
    if len(text) <= max_len:
        return [text]
    parts = []
    # Prefer splitting by double-newline paragraphs
    paras = text.split('\n\n')
    cur = []
    cur_len = 0
    for p in paras:
        p_with_sep = (p + '\n\n') if p else '\n\n'
        if cur_len + len(p_with_sep) <= max_len:
            cur.append(p_with_sep)
            cur_len += len(p_with_sep)
        else:
            if cur:
                parts.append(''.join(cur).rstrip())
            # If single paragraph too large, split by fixed chunk
            if len(p) > max_len:
                for i in range(0, len(p), max_len):
                    parts.append(p[i:i+max_len])
                cur = []
                cur_len = 0
            else:
                cur = [p_with_sep]
                cur_len = len(p_with_sep)
    if cur:
        parts.append(''.join(cur).rstrip())
    return parts

def get_apprise_instance():
    """
    Create and configure Apprise instance with URLs from settings and environment.
    Automatically includes user emails if SMTP is configured via environment.
    """
    import apprise
    
    apobj = apprise.Apprise()
    
    # Add URLs from settings and log add status
    apprise_urls = get_setting('apprise_urls', '')
    added = 0
    configured_urls = []
    for url in apprise_urls.strip().split('\n'):
        url = url.strip()
        if not url:
            continue
        try:
            ok = apobj.add(url)
            configured_urls.append(url)
            if ok:
                logger.info("Apprise: added URL: %s", url)
                added += 1
            else:
                logger.warning("Apprise: failed to add URL: %s", url)
        except Exception as e:
            logger.exception("Apprise: exception while adding URL %s: %s", url, e)


    # Add SMTP/Email if configured via environment variables (mailto scheme)
    smtp_server = os.environ.get('SMTP_SERVER')
    smtp_user = os.environ.get('SMTP_USER')
    smtp_password = os.environ.get('SMTP_PASSWORD')
    smtp_port = os.environ.get('SMTP_PORT', '587')
    smtp_from = os.environ.get('SMTP_FROM')

    if smtp_server and smtp_user and smtp_password and smtp_from:
        user_emails = get_user_emails()
        for email in user_emails:
            mailto_url = f"mailtos://{smtp_user}:{smtp_password}@{smtp_server}:{smtp_port}/?from={smtp_from}&to={email}"
            try:
                ok = apobj.add(mailto_url)
                if ok:
                    logger.info("Apprise: added SMTP mailto for %s", email)
                    added += 1
                else:
                    logger.warning("Apprise: failed to add SMTP mailto for %s", email)
            except Exception as e:
                logger.exception("Apprise: exception while adding SMTP mailto for %s: %s", email, e)

    if added == 0:
        logger.warning("Apprise: no services configured (apprise_urls empty and SMTP not configured) — notifications may be skipped")

    return apobj


def should_notify(event_type):
    """Check if notifications are enabled for this event type."""
    key = f"notify_on_{event_type}"
    value = get_setting(key, 'false')
    return value.lower() == 'true'


# Helper used to send notifications via Apprise with a retry and good logging
def _apprise_notify(apobj, title, body, body_format, attach=None, context=''):
    try:
        try:
            if attach:
                res = apobj.notify(title=title, body=body, body_format=body_format, attach=attach)
            else:
                res = apobj.notify(title=title, body=body, body_format=body_format)
        except Exception as e:
            logger.exception("Apprise: exception during notify (%s): %s", context, e)
            res = False

        if res:
            logger.info("Apprise: notification sent (%s)", context)
            return True

        # Retry once
        try:
            import time
            time.sleep(1)
            if attach:
                res2 = apobj.notify(title=title, body=body, body_format=body_format, attach=attach)
            else:
                res2 = apobj.notify(title=title, body=body, body_format=body_format)
            if res2:
                logger.info("Apprise: notification succeeded on retry (%s)", context)
                return True
            else:
                logger.error("Apprise: notification failed after retry (%s)", context)
                return False
        except Exception as e:
            logger.exception("Apprise: exception during retry (%s): %s", context, e)
            return False
    except Exception as e:
        logger.exception("Apprise: unexpected error in _apprise_notify (%s): %s", context, e)
        return False


def send_archive_notification(archive_config, job_id, stack_metrics, duration, total_size):
    """Send notification for archive job completion."""
    try:
        logger.info("Notifications: send_archive_notification called for archive=%s job=%s", archive_config.get('name') if archive_config else None, job_id)
    except Exception:
        pass

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
                    body += "    No archives older than configured retention were deleted.\n"
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
        temp_files = []  # track temp files we create so we can cleanup reliably
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
                        temp_files.append(tf.name)
                    finally:
                        tf.close()
        except Exception as e:
            logger.exception("Failed to prepare log attachment: %s", e)

        # Send notification: send plain-text (no HTML) for all non-email services and attach the full report/log
        try:
            raw_urls = [u.strip() for u in get_setting('apprise_urls', '').split('\n') if u.strip()]

            # Classify by URL scheme (email-like schemes: mailto, mailtos, smtp)
            from urllib.parse import urlparse
            email_urls = []
            non_email_urls = []
            for u in raw_urls:
                try:
                    p = urlparse(u)
                    scheme = (p.scheme or '').lower()
                except Exception:
                    scheme = ''
                if scheme.startswith('mailto') or scheme.startswith('mailtos') or 'mail' in scheme or 'smtp' in scheme:
                    email_urls.append(u)
                else:
                    non_email_urls.append(u)

            import apprise, tempfile

            def build_apprise_for(urls, include_smtp=True):
                a = apprise.Apprise()
                added_count = 0
                for u in urls:
                    try:
                        ok = a.add(u)
                        if ok:
                            logger.info("Apprise: added URL: %s", u)
                            added_count += 1
                        else:
                            logger.warning("Apprise: failed to add URL: %s", u)
                    except Exception as e:
                        logger.exception("Apprise: exception while adding URL %s: %s", u, e)
                if include_smtp:
                    smtp_server = os.environ.get('SMTP_SERVER')
                    smtp_user = os.environ.get('SMTP_USER')
                    smtp_password = os.environ.get('SMTP_PASSWORD')
                    smtp_port = os.environ.get('SMTP_PORT', '587')
                    smtp_from = os.environ.get('SMTP_FROM')
                    if smtp_server and smtp_user and smtp_password and smtp_from:
                        user_emails = get_user_emails()
                        for email in user_emails:
                            try:
                                mailto_url = f"mailtos://{smtp_user}:{smtp_password}@{smtp_server}:{smtp_port}/?from={smtp_from}&to={email}"
                                ok = a.add(mailto_url)
                                if ok:
                                    logger.info("Apprise: added SMTP mailto for %s", email)
                                    added_count += 1
                                else:
                                    logger.warning("Apprise: failed to add SMTP mailto for %s", email)
                            except Exception as e:
                                logger.exception("Apprise: exception while adding SMTP mailto for %s: %s", email, e)
                if added_count == 0:
                    logger.warning("Apprise: no services configured for this subset (urls=%s, include_smtp=%s)", urls, include_smtp)
                return a

            # Prepare a temp attachment (full job log preferred) for non-email services if needed
            attach_for_non_email = None
            temp_attach_created = False
            try:
                if non_email_urls:
                    # Prefer attaching the actual job log content (if available), otherwise fall back to a
                    # plain-text extraction of the notification body.
                    job_log_text = None
                    try:
                        job_log_text = job_row.get('log') if job_row else None
                    except Exception:
                        job_log_text = None

                    if job_log_text:
                        tf = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log', prefix=f'job_{job_id}_')
                        try:
                            tf.write(job_log_text)
                            tf.flush()
                            attach_for_non_email = tf.name
                            temp_files.append(tf.name)
                            temp_attach_created = True
                        finally:
                            tf.close()
                    else:
                        # Fallback: create a plain-text attachment by stripping HTML
                        if attach_path:
                            attach_for_non_email = attach_path
                        else:
                            tf = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log', prefix=f'notif_{job_id}_')
                            try:
                                try:
                                    text_body = strip_html_tags(body_to_send)
                                except Exception:
                                    text_body = body_to_send
                                tf.write(text_body)
                                tf.flush()
                                attach_for_non_email = tf.name
                                temp_files.append(tf.name)
                                temp_attach_created = True
                            finally:
                                tf.close()

                    # Build structured plain-text summary from available data (avoid CSS remnants)
                    try:
                        lines = []
                        lines.append(f"{status_emoji} Archive job completed: {archive_name}")
                        lines.append(f"Stacks: {success_count}/{stack_count} successful  |  Total size: {size_str}  |  Duration: {duration_str}")
                        lines.append("")

                        # SUMMARY OF CREATED ARCHIVES
                        if created_archives:
                            lines.append("SUMMARY OF CREATED ARCHIVES")
                            lines.append("")
                            for a in created_archives:
                                lines.append(f"{format_bytes(a['size'])} {a['path']}")
                            lines.append("")
                            lines.append(f"Total: {format_bytes(total_size)}")
                            lines.append("")

                        # DISK USAGE
                        try:
                            disk = get_disk_usage()
                            if disk and disk['total']:
                                lines.append("DISK USAGE (on /archives)")
                                lines.append("")
                                lines.append(f"Total: {format_bytes(disk['total'])}   Used: {format_bytes(disk['used'])} ({disk['percent']:.0f}% used)")
                                # Attempt to compute backup content size if available
                                try:
                                    total_archives_size = 0
                                    for root, dirs, files in __import__('os').walk(get_archives_path()):
                                        for fn in files:
                                            fp = __import__('os').path.join(root, fn)
                                            try:
                                                total_archives_size += __import__('os').path.getsize(fp)
                                            except Exception:
                                                continue
                                    lines.append(f"Backup Content Size (/archives): {format_bytes(total_archives_size)}")
                                except Exception:
                                    pass
                                lines.append("")
                        except Exception:
                            pass

                        # RETENTION SUMMARY
                        try:
                            if reclaimed is None:
                                lines.append("RETENTION SUMMARY")
                                lines.append("")
                                lines.append("No retention information available.")
                                lines.append("")
                            elif reclaimed == 0:
                                lines.append("RETENTION SUMMARY")
                                lines.append("")
                                lines.append("No archives older than configured retention were deleted.")
                                lines.append("")
                            else:
                                lines.append("RETENTION SUMMARY")
                                lines.append("")
                                lines.append(f"Freed space: {format_bytes(reclaimed)}")
                                lines.append("")
                        except Exception:
                            pass

                        # STACKS PROCESSED
                        if stack_metrics:
                            lines.append("STACKS PROCESSED")
                            lines.append("")
                            for metric in stack_metrics:
                                ok = '✓' if metric.get('status') == 'success' else '✗'
                                st_size = format_bytes(metric.get('archive_size_bytes') or 0)
                                archive_p = metric.get('archive_path') or 'N/A'
                                lines.append(f"{metric['stack_name']} {ok} {st_size} {archive_p}")
                            lines.append("")

                        # Named volumes warning
                        if stacks_with_volumes:
                            lines.append("⚠️ Named Volumes Warning")
                            lines.append("Named volumes are NOT included in the backup archives. Consider backing them up separately.")
                            lines.append("")
                            for metric in stacks_with_volumes:
                                volumes = metric.get('named_volumes') or []
                                lines.append(f"{metric['stack_name']}: {', '.join(volumes)}")
                            lines.append("")

                        compact_text = "\n".join(lines)

                        # Truncate to safe size for chat services (Discord message limit ~2000 chars)
                        max_len = 1800
                        if len(compact_text) > max_len:
                            compact_text = compact_text[:max_len] + "\n\n[Message truncated; full log attached]"

                    except Exception:
                        compact_text = f"Archive job completed: {archive_name}. See details: {base_url}/history?job={job_id}"

                    # Send plain-text + attachment to non-email services
                    # Detect Discord endpoints so we can split into multipart messages by section when needed
                    discord_urls = [u for u in non_email_urls if 'discord' in u.lower()]
                    other_non_email_urls = [u for u in non_email_urls if u not in discord_urls]

                    # Helper to split a long section into chunks respecting max_len
                    def split_section_by_length(text, max_len):
                        if len(text) <= max_len:
                            return [text]
                        parts = []
                        # Prefer splitting by double-newline paragraphs
                        paras = text.split('\n\n')
                        cur = []
                        cur_len = 0
                        for p in paras:
                            p_with_sep = (p + '\n\n') if p else '\n\n'
                            if cur_len + len(p_with_sep) <= max_len:
                                cur.append(p_with_sep)
                                cur_len += len(p_with_sep)
                            else:
                                if cur:
                                    parts.append(''.join(cur).rstrip())
                                # If single paragraph too large, split by fixed chunk
                                if len(p) > max_len:
                                    for i in range(0, len(p), max_len):
                                        parts.append(p[i:i+max_len])
                                    cur = []
                                    cur_len = 0
                                else:
                                    cur = [p_with_sep]
                                    cur_len = len(p_with_sep)
                        if cur:
                            parts.append(''.join(cur).rstrip())
                        return parts

                    try:
                        if discord_urls:
                            ap_disc = build_apprise_for(discord_urls, include_smtp=False)
                            max_len = 1800
                            if len(compact_text) <= max_len:
                                sent_non = _apprise_notify(ap_disc, title, compact_text, apprise.NotifyFormat.TEXT, attach=attach_for_non_email, context=f'non_email_{archive_name}_{job_id}')
                                if sent_non:
                                    logger.info("Apprise: sent compact text notification with attachment to Discord for archive=%s job=%s", archive_name, job_id)
                                else:
                                    logger.error("Apprise: Discord notification failed for archive=%s job=%s", archive_name, job_id)
                            else:
                                # Build section blocks
                                sections = []
                                # Header and summary
                                sections.append('\n'.join(lines[0:2]))
                                # Optional blocks
                                idx = 2
                                if created_archives:
                                    s = ['SUMMARY OF CREATED ARCHIVES', '']
                                    for a in created_archives:
                                        s.append(f"{format_bytes(a['size'])} {a['path']}")
                                    s.append('')
                                    s.append(f"Total: {format_bytes(total_size)}")
                                    sections.append('\n'.join(s))
                                # Disk usage block
                                try:
                                    disk = get_disk_usage()
                                    if disk and disk['total']:
                                        s = ['DISK USAGE (on /archives)', '', f"Total: {format_bytes(disk['total'])}   Used: {format_bytes(disk['used'])} ({disk['percent']:.0f}% used)"]
                                        try:
                                            total_archives_size = 0
                                            for root, dirs, files in __import__('os').walk(get_archives_path()):
                                                for fn in files:
                                                    fp = __import__('os').path.join(root, fn)
                                                    try:
                                                        total_archives_size += __import__('os').path.getsize(fp)
                                                    except Exception:
                                                        continue
                                            s.append(f"Backup Content Size (/archives): {format_bytes(total_archives_size)}")
                                        except Exception:
                                            pass
                                        sections.append('\n'.join(s))
                                except Exception:
                                    pass
                                # Retention
                                try:
                                    if reclaimed is None:
                                        sections.append('RETENTION SUMMARY\n\nNo retention information available.')
                                    elif reclaimed == 0:
                                        sections.append('RETENTION SUMMARY\n\nNo archives older than configured retention were deleted.')
                                    else:
                                        sections.append(f'RETENTION SUMMARY\n\nFreed space: {format_bytes(reclaimed)}')
                                except Exception:
                                    pass
                                # Stacks processed
                                if stack_metrics:
                                    s = ['STACKS PROCESSED', '']
                                    for metric in stack_metrics:
                                        ok = '✓' if metric.get('status') == 'success' else '✗'
                                        st_size = format_bytes(metric.get('archive_size_bytes') or 0)
                                        archive_p = metric.get('archive_path') or 'N/A'
                                        s.append(f"{metric['stack_name']} {ok} {st_size} {archive_p}")
                                    sections.append('\n'.join(s))
                                # Named volumes
                                if stacks_with_volumes:
                                    s = ['⚠️ Named Volumes Warning', 'Named volumes are NOT included in the backup archives. Consider backing them up separately.', '']
                                    for metric in stacks_with_volumes:
                                        volumes = metric.get('named_volumes') or []
                                        s.append(f"{metric['stack_name']}: {', '.join(volumes)}")
                                    sections.append('\n'.join(s))
                                # Footer
                                sections.append(f"View details: {base_url}/history?job={job_id}")

                                sent_any = False
                                import time, requests, json

                                # Helper: normalize possible discord apprise schemes to webhook URL
                                def normalize_discord_webhook(u):
                                    try:
                                        low = u.lower()
                                        if low.startswith('discord://'):
                                            # apprise style: discord://<id>/<token>
                                            return 'https://discord.com/api/webhooks/' + u.split('://', 1)[1].lstrip('/')
                                        if 'discord' in low and '/webhooks/' in low:
                                            # likely a full https url already
                                            if low.startswith('http'):
                                                return u
                                            else:
                                                return 'https://' + u
                                        # Fallback: return original
                                        return u
                                    except Exception:
                                        return u

                                # Build embeds from sections; each embed description <= 4096 chars, title <=256
                                embeds = []
                                for sec in sections:
                                    sec_lines = sec.split('\n')
                                    title_line = sec_lines[0] if sec_lines else ''
                                    desc = '\n'.join(sec_lines[1:]).strip() if len(sec_lines) > 1 else ''
                                    # Split desc into chunks of <= 4000 to be safe
                                    max_desc = 4000
                                    if not desc:
                                        desc = ''
                                    if len(desc) <= max_desc:
                                        embeds.append({'title': title_line[:250], 'description': desc})
                                    else:
                                        # Split by paragraphs
                                        paras = desc.split('\n\n')
                                        cur = ''
                                        for p in paras:
                                            piece = (p + '\n\n')
                                            if len(cur) + len(piece) <= max_desc:
                                                cur += piece
                                            else:
                                                if cur:
                                                    embeds.append({'title': title_line[:250], 'description': cur.rstrip()})
                                                # large single paragraph? split it raw
                                                if len(piece) > max_desc:
                                                    for i in range(0, len(piece), max_desc):
                                                        chunk = piece[i:i+max_desc]
                                                        embeds.append({'title': title_line[:250], 'description': chunk})
                                                    cur = ''
                                                else:
                                                    cur = piece
                                        if cur:
                                            embeds.append({'title': title_line[:250], 'description': cur.rstrip()})

                                # Send in batches of up to 10 embeds per webhook request
                                for webhook in discord_urls:
                                    wh_url = normalize_discord_webhook(webhook)
                                    try:
                                        batch_size = 10
                                        total_sent = 0
                                        for i in range(0, len(embeds), batch_size):
                                            batch = embeds[i:i+batch_size]
                                            payload = {'embeds': [{k: v for k, v in e.items() if v} for e in batch]}
                                            # Attach log only on final batch
                                            attach_file = attach_for_non_email if (i + batch_size >= len(embeds)) else None
                                            headers = {'Content-Type': 'application/json'}
                                            if attach_file:
                                                # multipart: payload_json + file
                                                try:
                                                    with open(attach_file, 'rb') as fh:
                                                        files = {'file': (attach_file.split('/')[-1], fh)}
                                                        data = {'payload_json': json.dumps(payload)}
                                                        r = requests.post(wh_url, data=data, files=files, timeout=10)
                                                except Exception as fe:
                                                    logger.exception("Apprise/Discord: failed to attach/send file to %s: %s", wh_url, fe)
                                                    r = None
                                            else:
                                                try:
                                                    r = requests.post(wh_url, json=payload, headers=headers, timeout=10)
                                                except Exception as re:
                                                    logger.exception("Apprise/Discord: request to %s failed: %s", wh_url, re)
                                                    r = None

                                            ok = False
                                            if r is not None:
                                                try:
                                                    if 200 <= r.status_code < 300:
                                                        ok = True
                                                    else:
                                                        logger.warning("Apprise/Discord: webhook %s returned status %s: %s", wh_url, r.status_code, getattr(r, 'text', ''))
                                                except Exception:
                                                    logger.exception("Apprise/Discord: could not interpret response from %s", wh_url)
                                            if ok:
                                                total_sent += 1
                                            # small pause to avoid rate limits
                                            time.sleep(0.25)
                                        if total_sent > 0:
                                            sent_any = True
                                            logger.info("Apprise/Discord: sent %s embed batch messages to %s for archive=%s job=%s", total_sent, wh_url, archive_name, job_id)
                                        else:
                                            logger.error("Apprise/Discord: failed to send any embeds to %s for archive=%s job=%s", wh_url, archive_name, job_id)
                                    except Exception as e:
                                        logger.exception("Apprise/Discord: exception while sending embeds to %s: %s", webhook, e)

                                if sent_any:
                                    logger.info("Apprise: sent multipart embed notification to Discord for archive=%s job=%s (embeds=%s)", archive_name, job_id, len(embeds))
                                else:
                                    logger.error("Apprise: multipart embed Discord notification failed for archive=%s job=%s", archive_name, job_id)

                        # Non-discord non-email services: send as a single message (truncate if needed)
                        if other_non_email_urls:
                            ap_non_other = build_apprise_for(other_non_email_urls, include_smtp=False)
                            message_text = compact_text
                            max_len_other = 1500
                            if len(message_text) > max_len_other:
                                message_text = message_text[:max_len_other] + "\n\n[Message truncated; full log attached]"
                            try:
                                sent_other = _apprise_notify(ap_non_other, title, message_text, apprise.NotifyFormat.TEXT, attach=attach_for_non_email, context=f'non_email_other_{archive_name}_{job_id}')
                                if sent_other:
                                    logger.info("Apprise: sent compact text notification with attachment to non-email services (non-Discord) for archive=%s job=%s", archive_name, job_id)
                                else:
                                    logger.error("Apprise: non-email (non-Discord) notification failed for archive=%s job=%s", archive_name, job_id)
                            except Exception as e:
                                logger.exception("Apprise: exception while sending non-email (non-Discord) notification for %s job %s: %s", archive_name, job_id, e)
                    except Exception as e:
                        logger.exception("Apprise: exception while sending non-email notification for %s job %s: %s", archive_name, job_id, e)

                # Send full notification to email (SMTP) services
                if email_urls or os.environ.get('SMTP_SERVER'):
                    ap_email = build_apprise_for(email_urls, include_smtp=True)
                    try:
                        sent_email = _apprise_notify(ap_email, title, send_body, body_format, attach=attach_path, context=f'email_{archive_name}_{job_id}')
                        if sent_email:
                            logger.info("Apprise: sent full notification to email services for archive=%s job=%s", archive_name, job_id)
                        else:
                            logger.error("Apprise: email notification failed for archive=%s job=%s", archive_name, job_id)
                    except Exception as e:
                        logger.exception("Apprise: exception while sending email notification for %s job %s: %s", archive_name, job_id, e)

                # If no services configured at all, fall back to original apobj send for compatibility
                if not non_email_urls and not email_urls and not os.environ.get('SMTP_SERVER'):
                    try:
                        sent = _apprise_notify(apobj, title, send_body, body_format, attach=attach_path, context=f'archive_{archive_name}_{job_id}')
                        if not sent:
                            logger.error("Apprise: notification failed for archive=%s job=%s", archive_name, job_id)
                    except Exception as e:
                        logger.exception("Apprise: exception while sending archive notification for %s job %s: %s", archive_name, job_id, e)

            finally:
                try:
                    import os
                    # Remove any temp files we created
                    try:
                        for p in list(temp_files):
                            if p and os.path.exists(p):
                                os.unlink(p)
                    except Exception:
                        pass
                    # Clean up non-email temp attach if distinct
                    if temp_attach_created and attach_for_non_email and os.path.exists(attach_for_non_email) and attach_for_non_email != attach_path:
                        try:
                            os.unlink(attach_for_non_email)
                        except Exception:
                            pass
                    # Also cleanup attach_path if we created it
                    if attach_path and os.path.exists(attach_path):
                        try:
                            os.unlink(attach_path)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception as e:
            logger.exception("Apprise: unexpected error sending notifications for %s job %s: %s", archive_name, job_id, e)        
    except Exception as e:
        logger.exception("Failed to send notification: %s", e)


def send_retention_notification(archive_name, deleted_count, deleted_dirs, deleted_files, reclaimed_bytes):
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
<strong>Archives deleted:</strong> {deleted_count} <small>({deleted_dirs} dirs, {deleted_files} files)</small><br>
<strong>Space freed:</strong> {size_str}
</p>
<hr>
<p><small>Docker Archiver: <a href=\"{base_url}\">{base_url}</a></small></p>"""
        
        # Get format preference
        body_format = get_notification_format()
        
        # Convert to plain text if needed
        import apprise
        if body_format == apprise.NotifyFormat.TEXT:
            body = strip_html_tags(body)
        
        # Send notification via centralized helper
        try:
            _ = _apprise_notify(apobj, title, body, body_format, context=f'retention_{archive_name}')
        except Exception as e:
            logger.exception("Failed to send retention notification: %s", e)
        
    except Exception as e:
        logger.exception("Failed to send notification: %s", e)

def send_permissions_fix_notification(result, report_path=None):
    """Send notification after a permissions fix run.

    `result` is the dict returned from `apply_permissions_recursive`.
    `report_path`, if provided, will be attached to the notification as a TXT file (full report).

    The email contains totals and a per-stack summary derived from the collected samples (if any).
    """
    if not should_notify('success'):
        # Clean up the report file if present (best-effort)
        try:
            if report_path and os.path.exists(report_path):
                os.unlink(report_path)
        except Exception:
            pass
        return

    try:
        apobj = get_apprise_instance()
        if not apobj:
            # Cleanup
            try:
                if report_path and os.path.exists(report_path):
                    os.unlink(report_path)
            except Exception:
                pass
            return

        base = get_archives_path()

        # Extract samples from result (may be truncated)
        fixed_files = (result.get('fixed_files') or [])[:200]
        fixed_dirs = (result.get('fixed_dirs') or [])[:200]

        # Group files/dirs by stack using same logic as check_permissions.
        # Keep small sample lists for display (limited) and counters for accurate totals.
        from collections import defaultdict
        stacks = defaultdict(lambda: {'files': [], 'dirs': [], 'file_count': 0, 'dir_count': 0})

        def _stack_key(p):
            try:
                rel = os.path.relpath(p, base)
            except Exception:
                rel = p
            parts = rel.split(os.sep) if rel and not rel.startswith('..') else []
            top = parts[0] if len(parts) >= 1 else ''
            stack = parts[1] if len(parts) >= 2 else '<root>'
            if stack == '<root>':
                display = top or base
            else:
                display = f"{top}/{stack}"
            return display

        # Populate stacks from available in-memory samples (if any)
        for p in fixed_files:
            s = stacks[_stack_key(p)]
            if len(s['files']) < 5:
                s['files'].append(p)
            s['file_count'] += 1
        for p in fixed_dirs:
            s = stacks[_stack_key(p)]
            if len(s['dirs']) < 5:
                s['dirs'].append(p)
            s['dir_count'] += 1

        # If no in-memory samples, try to parse the report file to build per-stack counts (best-effort)
        if not fixed_files and not fixed_dirs and report_path:
            try:
                with open(report_path, 'r', encoding='utf-8') as rf:
                    for line in rf:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if line.startswith('F\t'):
                            p = line[2:]
                            s = stacks[_stack_key(p)]
                            if len(s['files']) < 5:
                                s['files'].append(p)
                            s['file_count'] += 1
                        elif line.startswith('D\t'):
                            p = line[2:]
                            s = stacks[_stack_key(p)]
                            if len(s['dirs']) < 5:
                                s['dirs'].append(p)
                            s['dir_count'] += 1
            except Exception:
                # Best-effort: if report can't be read, fall back to available data
                pass

        # Prefer totals reported in result (full counts); fall back to parsed/sample sums
        total_files = None
        total_dirs = None
        try:
            if result and isinstance(result, dict):
                total_files = int(result.get('files_changed')) if result.get('files_changed') is not None else None
                total_dirs = int(result.get('dirs_changed')) if result.get('dirs_changed') is not None else None
        except Exception:
            total_files = None
            total_dirs = None

        if total_files is None:
            total_files = sum(v.get('file_count', len(v.get('files', []))) for v in stacks.values())
        if total_dirs is None:
            total_dirs = sum(v.get('dir_count', len(v.get('dirs', []))) for v in stacks.values())

        title = get_subject_with_tag(f"🔧 Permissions Fixed: {total_files} files, {total_dirs} dirs")

        css = """
        <style>
        .da-table { width: 100%; border-collapse: collapse; font-family: monospace; }
        .da-table th, .da-table td { padding: 6px 8px; border-bottom: 1px solid #eee; text-align: left; }
        .da-small { font-size: 90%; color: #666; }
        .da-stack { margin-bottom: 12px; }
        </style>
        """

        body = f"""
{css}
<div style='font-family: Arial, Helvetica, sans-serif; max-width:800px; margin:0; text-align:left; color:#222;'>
  <h2 style='margin-bottom:6px;'>🔧 Permissions Fix Completed</h2>
  <p class='da-small'><strong>Files fixed:</strong> {total_files} &nbsp;|&nbsp; <strong>Dirs fixed:</strong> {total_dirs}</p>
"""
        # Per-stack sections (summary only; full report attached separately)
        if stacks:
            body += "\n  <h3>FIXED ITEMS BY STACK (summary)</h3>\n"
            body += "  <ul>\n"
            for stack_name in sorted(stacks.keys()):
                s = stacks[stack_name]
                body += f"    <li><strong>{stack_name}</strong>: {len(s['files'])} file(s), {len(s['dirs'])} dir(s)</li>\n"
            body += "  </ul>\n"
            body += "  <p class='da-small'>A full report has been attached to this notification (if available).</p>\n"
        else:
            body += "  <p><em>No fixes were necessary.</em></p>\n"

        body += "\n  <p class='da-small'>Docker Archiver</p>\n</div>\n"

        # Send as HTML unless user prefers text
        body_format = get_notification_format()
        import apprise
        if body_format == apprise.NotifyFormat.TEXT:
            send_body = strip_html_tags(body)
        else:
            send_body = body

        # Attach full report if it exists
        attach_path = None
        try:
            if report_path and os.path.exists(report_path):
                attach_path = report_path
        except Exception:
            attach_path = None

        if attach_path:
            try:
                sent = _apprise_notify(apobj, title, send_body + "\n\n(Full report attached)", body_format, attach=attach_path, context='permissions_fix')
                if sent:
                    try:
                        os.unlink(attach_path)
                    except Exception:
                        pass
            except Exception as e:
                logger.exception("Failed to send permissions notification with attachment: %s", e)
        else:
            try:
                _ = _apprise_notify(apobj, title, send_body, body_format, context='permissions_fix')
            except Exception as e:
                logger.exception("Failed to send permissions notification: %s", e)
    except Exception as e:
        logger = get_logger(__name__)
        logger.exception("Failed to send permissions notification: %s", e)



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
<p><small>Docker Archiver: <a href=\"{base_url}\">{base_url}</a></small></p>"""
        
        # Get format preference
        body_format = get_notification_format()
        
        # Convert to plain text if needed
        import apprise
        if body_format == apprise.NotifyFormat.TEXT:
            body = strip_html_tags(body)
        
        # Send notification via centralized helper
        try:
            _ = _apprise_notify(apobj, title, body, body_format, context='error_notification')
        except Exception as e:
            logger.exception("Failed to send error notification: %s", e)
        
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
        
        # Send notification via centralized helper
        try:
            _ = _apprise_notify(apobj, title, body, body_format, context='test_notification')
        except Exception as e:
            raise Exception(f"Failed to send test notification: {e}")
        
    except Exception as e:
        raise Exception(f"Failed to send test notification: {e}")

"""
Notification system (SMTP-only): all notifications are sent via SMTP using settings stored in the database.
"""
import os
from app.db import get_db
from app.utils import format_bytes, format_duration, get_disk_usage, get_archives_path, get_logger, setup_logging

# Configuration constants for notifications (SMTP-focused)
NOTIFY_EMAIL_MAX_BODY = 10000  # conservative limit for email bodies

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




from .formatters import (
    strip_html_tags,
    build_compact_text,
    split_section_by_length,
    build_short_body,
    build_full_body,
    build_sections,
    build_section_html,
)


# Discord/webhook URL normalization removed — project is SMTP-only now.


# Only SMTP is supported now. All non-SMTP transports were removed.
# Legacy Apprise-related helpers have been removed to simplify the code path.

def get_notification_format():
    """Return preferred notification format for legacy callers.

    We use HTML for all emails (SMTP) and don't support chat formats anymore.
    """
    return 'html'

def should_notify(event_type):
    """Check if notifications are enabled for this event type."""
    key = f"notify_on_{event_type}"
    value = get_setting(key, 'false')
    return value.lower() == 'true'


# Helper used to send notifications via Apprise with a retry and good logging
# Apprise notify helper removed with Apprise removal.

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
        
        # SMTP-only: legacy Apprise URLs are ignored; proceed to build message and send via SMTP
        
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
        
        # Created archives table (alphabetical by filename)
        created_archives = []
        for m in stack_metrics:
            path = m.get('archive_path')
            size = m.get('archive_size_bytes') or 0
            if path:
                created_archives.append({'path': str(path), 'size': size})

        # Fetch job retention/log info
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT reclaimed_bytes, log FROM jobs WHERE id = %s;", (job_id,))
                job_row = cur.fetchone()
                reclaimed = job_row.get('reclaimed_bytes') if job_row else None
                job_log = job_row.get('log') if job_row else ''
        except Exception:
            reclaimed = None
            job_log = ''

        # Build HTML body using helpers
        body = build_full_body(
            archive_name=archive_name,
            status_emoji=status_emoji,
            success_count=success_count,
            stack_count=stack_count,
            size_str=size_str,
            duration_str=duration_str,
            stack_metrics=stack_metrics,
            created_archives=created_archives,
            total_size=total_size,
            reclaimed=reclaimed,
            job_log=job_log,
            base_url=base_url,
            stacks_with_volumes=stacks_with_volumes,
            job_id=job_id,
            include_log_inline=True,
        )

        # user settings for attachments
        attach_log_setting = get_setting('notify_attach_log', 'false').lower() == 'true'
        attach_on_failure_setting = get_setting('notify_attach_log_on_failure', 'false').lower() == 'true'

        # Always build full HTML body for emails and a structured sections list for chat services
        should_attach_log = attach_log_setting or (attach_on_failure_setting and failed_count > 0)
        if should_attach_log:
            html_body_to_send = build_full_body(
                archive_name=archive_name,
                status_emoji=status_emoji,
                success_count=success_count,
                stack_count=stack_count,
                size_str=size_str,
                duration_str=duration_str,
                stack_metrics=stack_metrics,
                created_archives=created_archives,
                total_size=total_size,
                reclaimed=reclaimed,
                job_log=job_log,
                base_url=base_url,
                stacks_with_volumes=stacks_with_volumes,
                job_id=job_id,
                include_log_inline=False,
            )
        else:
            html_body_to_send = body

        # Build compact text and sections (plain-text) for non-email services
        compact_text, lines = build_compact_text(archive_name, stack_metrics, created_archives, total_size, size_str, duration_str, stacks_with_volumes, reclaimed, base_url)
        sections = build_sections(archive_name, lines, created_archives, total_size, stack_metrics, stacks_with_volumes, reclaimed, base_url, job_id)



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
                    import tempfile
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

        # We no longer support non-email (Apprise) transports. Only email via SMTP is used.
        # Prepare an attachment (job log) for the email if available.
        import tempfile
        attach_path_for_email = None
        try:
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
                    attach_path_for_email = tf.name
                    temp_files.append(tf.name)
                finally:
                    tf.close()
            else:
                if attach_path:
                    attach_path_for_email = attach_path
        except Exception as e:
            logger.exception("Failed to prepare job log attachment for email: %s", e)

        # set attach_path to the prepared email attachment
        attach_path = attach_path_for_email

        # Legacy Apprise URLs and mailto/mailtos are ignored (SMTP-only project).
        # If you previously relied on Apprise mailto URLs, migrate recipients to user profiles or the Notifications settings page.

        # We only support SMTP delivery now. Send full notification via SMTP to configured recipients.
        from app.notifications.adapters import SMTPAdapter
        smtp_adapter = SMTPAdapter() if get_setting('smtp_server') else None
        if not smtp_adapter:
            logger.warning("SMTP not configured; skipping notification for archive=%s job=%s", archive_name, job_id)
            return

        send_body = html_body_to_send
        # Recipients resolved from user profiles
        recipients = get_user_emails() or []
        if not recipients:
            logger.warning("No recipients configured for notification (no user emails). archive=%s job=%s", archive_name, job_id)
            return

        try:
            # send via SMTPAdapter to recipients
            res = smtp_adapter.send(title, send_body, body_format=None, attach=attach_path, recipients=recipients, context=f'email_smtp_{archive_name}_{job_id}')
            if res.success:
                logger.info("SMTP adapter: sent full notification via SMTP for archive=%s job=%s", archive_name, job_id)
            else:
                logger.error("SMTP adapter: send failed for archive=%s job=%s: %s", archive_name, job_id, res.detail)
        except Exception as e:
            logger.exception("Error while sending email notifications for %s job %s: %s", archive_name, job_id, e)
        finally:
            try:
                # Remove any temp files we created
                try:
                    for p in list(temp_files):
                        if p and os.path.exists(p):
                            os.unlink(p)
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
        logger.exception("Failed to send notification: %s", e)


def send_retention_notification(archive_name, deleted_count, deleted_dirs, deleted_files, reclaimed_bytes):
    """Send notification for retention job completion."""
    if not should_notify('success'):
        return

    try:
        # SMTP-only: Build message and send via SMTP
        
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
        if body_format == 'text':
            body = strip_html_tags(body)
        
        # Send via SMTP
        try:
            from app.notifications.adapters import SMTPAdapter
            smtp_adapter = SMTPAdapter() if get_setting('smtp_server') else None
            if not smtp_adapter:
                logger.warning("SMTP not configured; skipping retention notification for %s", archive_name)
            else:
                res = smtp_adapter.send(title, body, body_format=None, attach=None, recipients=get_user_emails(), context=f'retention_{archive_name}')
                if not res.success:
                    logger.error("SMTP send failed for retention notification: %s", res.detail)
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
        # SMTP-only: build email and send via SMTPAdapter
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
        if body_format == 'text':
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

        try:
            from app.notifications.adapters import SMTPAdapter
            smtp_adapter = SMTPAdapter() if get_setting('smtp_server') else None
            if not smtp_adapter:
                logger.warning('SMTP not configured; permissions notification skipped')
            else:
                try:
                    if attach_path:
                        res = smtp_adapter.send(title, send_body + "\n\n(Full report attached)", body_format=None, attach=attach_path, recipients=get_user_emails(), context='permissions_fix')
                        if res.success:
                            try:
                                os.unlink(attach_path)
                            except Exception:
                                pass
                        else:
                            logger.error('SMTP permissions notification send failed: %s', res.detail)
                    else:
                        res = smtp_adapter.send(title, send_body, body_format=None, attach=None, recipients=get_user_emails(), context='permissions_fix')
                        if not res.success:
                            logger.error('SMTP permissions notification send failed: %s', res.detail)
                except Exception as e:
                    logger.exception("Failed to send permissions notification: %s", e)
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
        base_url = get_setting('base_url', 'http://localhost:8080')
        
        title = get_subject_with_tag(f"❌ Archive Failed: {archive_name}")
        body = f"""<h2>Archive job failed for: {archive_name}</h2>
<p>
<strong>Error:</strong><br>
<code>{error_message}</code>
</p>
<hr>
<p><small>Docker Archiver: <a href=\"{base_url}\">{base_url}</a></small></p>"""

        # Send via SMTP
        try:
            from app.notifications.adapters import SMTPAdapter
            smtp_adapter = SMTPAdapter() if get_setting('smtp_server') else None
            if not smtp_adapter:
                logger.warning("SMTP not configured; skipping error notification for %s", archive_name)
                return
            res = smtp_adapter.send(title, body, body_format=None, attach=None, recipients=get_user_emails(), context='error_notification')
            if not res.success:
                logger.error("SMTP send failed for error notification: %s", res.detail)
        except Exception as e:
            logger.exception("Failed to send error notification: %s", e)
    except Exception as e:
        logger.exception("Failed to send notification: %s", e)


def send_test_notification():
    """Send a test notification to verify SMTP configuration and recipients."""
    base_url = get_setting('base_url', 'http://localhost:8080')
    title = get_subject_with_tag("🔔 Docker Archiver - Test Notification")

    # Compose HTML body for email
    body_html = f"""<h2>Test Notification from Docker Archiver</h2>
<p>If you received this message, your notification configuration is working correctly!</p>
<div style='border-top:1px solid #eee;margin:8px 0'></div>
<p><small>Docker Archiver: <a href=\"{base_url}\">{base_url}</a></small></p>"""

    # Use SMTPAdapter to send when configured
    try:
        from app.notifications.adapters import SMTPAdapter
        smtp_adapter = SMTPAdapter() if get_setting('smtp_server') else None
        if not smtp_adapter:
            raise Exception('SMTP not configured')

        res = smtp_adapter.send(title, body_html, body_format=None, attach=None, context='test_notification')
        if not res.success:
            raise Exception(f"SMTP send failed: {res.detail}")

    except Exception as e:
        raise Exception(f"Failed to send test notification: {e}")

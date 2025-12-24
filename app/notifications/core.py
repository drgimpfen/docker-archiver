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




from .formatters import (
    strip_html_tags,
    build_compact_text,
    split_section_by_length,
    build_short_body,
    build_full_body,
    build_sections,
    build_section_html,
)


def get_apprise_instance():
    """
    Create and configure Apprise instance with URLs from settings.
    Email delivery should be configured via Apprise mailto/mailtos URLs in settings (Settings → Notifications).
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


    # NOTE: Email sending is handled by MailtoAdapter (uses mailto/mailtos Apprise URLs defined in settings).

    if added == 0:
        logger.warning("Apprise: no services configured (apprise_urls empty) — notifications may be skipped")

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

            import tempfile

            # Use adapters for sending to avoid duplicating apprise logic.
            # Detect Discord endpoints so we can instantiate a DiscordAdapter and
            # separate them from other non-email services.
            discord_urls = [u for u in non_email_urls if 'discord' in u.lower()]
            other_non_email_urls = [u for u in non_email_urls if u not in discord_urls]

            from app.notifications.adapters import GenericAdapter, DiscordAdapter, MailtoAdapter

            # All email-like URLs (mailto, mailtos, smtp, etc.) are handled by MailtoAdapter to avoid duplicate sends
            # and centralize email-specific handling.
            discord_adapter = DiscordAdapter(webhooks=discord_urls) if discord_urls else None
            generic = GenericAdapter(urls=other_non_email_urls) if other_non_email_urls else None
            explicit_email_adapter = None
            # MailtoAdapter handles sending to any email-like Apprise URLs (we pass email_urls directly)
            mailto_adapter = MailtoAdapter(urls=email_urls if email_urls else None)

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

                    # Build structured plain-text summary using helper
                    try:
                        compact_text, lines = build_compact_text(archive_name, stack_metrics, created_archives, total_size, size_str, duration_str, stacks_with_volumes, reclaimed, base_url)
                    except Exception:
                        compact_text = f"Archive job completed: {archive_name}. See details: {base_url}/history?job={job_id}"
                        lines = [compact_text]

                    # Send plain-text + attachment to non-email services
                    # Detect Discord endpoints so we can split into multipart messages by section when needed
                    discord_urls = [u for u in non_email_urls if 'discord' in u.lower()]
                    other_non_email_urls = [u for u in non_email_urls if u not in discord_urls]


                    try:
                        if discord_urls and discord_adapter:
                            # Build embed metadata
                            status_color = 0x2ECC71 if failed_count == 0 else 0xE67E22
                            embed_fields = [
                                {'name': 'Stacks', 'value': f"{success_count}/{stack_count}", 'inline': True},
                                {'name': 'Total Size', 'value': size_str, 'inline': True},
                                {'name': 'Duration', 'value': duration_str, 'inline': True}
                            ]
                            footer_text = f"Job {job_id} — Archive: {archive_name}"
                            emb_opts = {'color': status_color, 'footer': footer_text, 'fields': embed_fields}

                            max_len = 1800
                            # Delegate all Discord sending to the centralized helper (handles single and sectioned sends)
                            try:
                                from app.notifications.discord_dispatch import send_to_discord
                                sections = build_sections(archive_name, lines, created_archives, total_size, stack_metrics, stacks_with_volumes, reclaimed, base_url, job_id)
                                view_url = f"{base_url}/history?job={job_id}"
                                # Construct Markdown body by joining sections (Discord prefers Markdown embeds)
                                md_body = "\n\n".join(sections)
                                # Log configured discord endpoints for easier debugging of duplicate posts
                                try:
                                    logger.info("Discord: configured webhooks count=%d", len(discord_urls))
                                except Exception:
                                    pass
                                result = send_to_discord(discord_adapter, title, md_body, compact_text, sections, attach_for_non_email, embed_options=emb_opts, max_desc=NOTIFY_EMBED_DESC_MAX, view_url=view_url)
                                if result.get('sent_any'):
                                    logger.info("Discord adapter: sent notifications to Discord for archive=%s job=%s", archive_name, job_id)
                                else:
                                    logger.error("Discord adapter: sends failed for archive=%s job=%s: %s", archive_name, job_id, result.get('details'))
                            except Exception as e:
                                logger.exception("Discord adapter: exception while sending notifications for %s job %s: %s", archive_name, job_id, e)

                        # Non-discord non-email services: send as a single message (truncate if needed)
                        if other_non_email_urls and _adapter:
                            message_text = compact_text
                            max_len_other = 1500
                            if len(message_text) > max_len_other:
                                message_text = message_text[:max_len_other] + "\n\n[Message truncated; full log attached]"
                            try:
                                # Use Markdown for non-SMTP (chat) notification bodies
                                res = _adapter.send(title, message_text, body_format=__import__('apprise').NotifyFormat.MARKDOWN, attach=attach_for_non_email, context=f'non_email_other_{archive_name}_{job_id}')
                                if res.success:
                                    logger.info("Generic adapter: sent compact markdown notification with attachment to non-email services (non-Discord) for archive=%s job=%s", archive_name, job_id)
                                else:
                                    logger.error("Generic adapter: non-email (non-Discord) notification failed for archive=%s job=%s: %s", archive_name, job_id, res.detail)
                            except Exception as e:
                                logger.exception("Generic adapter: exception while sending non-email (non-Discord) notification for %s job %s: %s", archive_name, job_id, e)
                    except Exception as e:
                        logger.exception("Apprise: exception while sending non-email notification for %s job %s: %s", archive_name, job_id, e)

                # Send full notification to email services.
                # For SMTP/email targets we use the HTML body
                import apprise
                send_body = html_body_to_send
                body_format = apprise.NotifyFormat.HTML

                email_sent_any = False
                try:
                    # Send all email-like URLs via the MailtoAdapter (centralized email handling)
                    if mailto_adapter:
                        res = mailto_adapter.send(title, send_body, body_format, attach=attach_path, context=f'email_mailto_{archive_name}_{job_id}')
                        if res.success:
                            email_sent_any = True
                            logger.info("Mailto adapter: sent full notification via mailto/mailtos/smtp for archive=%s job=%s", archive_name, job_id)
                        else:
                            logger.error("Mailto adapter: mailto send failed for archive=%s job=%s: %s", archive_name, job_id, res.detail)

                    if not email_sent_any:
                        logger.warning("No email targets delivered for archive=%s job=%s", archive_name, job_id)
                except Exception as e:
                    logger.exception("Error while sending email notifications for %s job %s: %s", archive_name, job_id, e)

                # If no services configured at all, fall back to original apobj send for compatibility
                if not non_email_urls and not email_urls:
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
    """Send a test notification to verify configuration.

    Behavior:
    - Sends **HTML** to email-like Apprise URLs (mailto/mailtos)
    - Sends **Markdown** to all other Apprise URLs (Discord, Telegram, etc.)
    """
    try:
        # Read configured Apprise URLs and split into email vs non-email targets
        import apprise
        raw = get_setting('apprise_urls', '').strip()
        if not raw:
            raise Exception("No notification services configured")

        email_urls = []
        other_urls = []
        for u in raw.split('\n'):
            u = u.strip()
            if not u:
                continue
            ul = u.lower()
            if ul.startswith('mailto') or ul.startswith('mailtos'):
                email_urls.append(u)
            else:
                other_urls.append(u)

        base_url = get_setting('base_url', 'http://localhost:8080')
        title = get_subject_with_tag("🔔 Docker Archiver - Test Notification")

        # Compose HTML body for email
        body_html = f"""<h2>Test Notification from Docker Archiver</h2>
<p>If you received this message, your notification configuration is working correctly!</p>
<div style='border-top:1px solid #eee;margin:8px 0'></div>
<p><small>Docker Archiver: <a href=\"{base_url}\">{base_url}</a></small></p>"""

        # Compose Markdown body for chat services
        md_body = f"## Test Notification from Docker Archiver\n\nIf you received this message, your notification configuration is working correctly!\n\nDocker Archiver: [{base_url}]({base_url})"

        sent_any = False
        errors = []

        # Send to non-email (chat) services as Markdown
        if other_urls:
            apobj_other = apprise.Apprise()
            added = 0
            for u in other_urls:
                try:
                    ok = apobj_other.add(u)
                    added += 1 if ok else 0
                except Exception as e:
                    logger.exception("Failed to add Apprise URL %s: %s", u, e)
            if added > 0:
                try:
                    _ = _apprise_notify(apobj_other, title, md_body, apprise.NotifyFormat.MARKDOWN, context='test_notification_non_email')
                    sent_any = True
                except Exception as e:
                    errors.append(str(e))

        # Send to email (mailto/mailtos) services as HTML
        if email_urls:
            apobj_mail = apprise.Apprise()
            added = 0
            for u in email_urls:
                try:
                    ok = apobj_mail.add(u)
                    added += 1 if ok else 0
                except Exception as e:
                    logger.exception("Failed to add Apprise URL %s: %s", u, e)
            if added > 0:
                try:
                    _ = _apprise_notify(apobj_mail, title, body_html, apprise.NotifyFormat.HTML, context='test_notification_email')
                    sent_any = True
                except Exception as e:
                    errors.append(str(e))

        if not sent_any:
            raise Exception(f"No test notifications sent: {errors or ['no configured services']}" )

    except Exception as e:
        raise Exception(f"Failed to send test notification: {e}")

"""Notification-specific formatting helpers.

Includes HTML->text conversion, compact text builder and section splitting
logic used when crafting messages for chat services (Discord, etc.).
"""
from typing import List, Tuple
import re
from app.utils import format_bytes, get_disk_usage, get_archives_path


def strip_html_tags(html_text: str) -> str:
    """Convert HTML to plain text by removing tags and converting entities.

    Also removes <style> and <script> blocks to avoid leaving CSS/JS content behind.
    """
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


def split_section_by_length(text: str, max_len: int) -> List[str]:
    """Split a long text into parts respecting paragraph boundaries where possible."""
    if not text:
        return ['']
    if len(text) <= max_len:
        return [text]
    parts: List[str] = []
    paras = text.split('\n\n')
    cur: List[str] = []
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


def build_compact_text(archive_name: str, stack_metrics: List[dict], created_archives: List[dict], total_size: int, size_str: str, duration_str: str, stacks_with_volumes: List[dict], reclaimed, base_url: str) -> Tuple[str, List[str]]:
    lines: List[str] = []
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
    if len(compact_text) > 1800:
        compact_text = compact_text[:1800] + "\n\n[Message truncated; full log attached]"

    return compact_text, lines


def build_short_body(archive_name: str, status_emoji: str, success_count: int, stack_count: int, size_str: str, duration_str: str, stack_metrics: list, base_url: str, job_id: int) -> str:
    """Build a compact HTML short body for notifications (used for short verbosity).

    Returns an HTML string suitable for embedding in chat services or email.
    """
    short_body = f"<h2>{status_emoji} Archive: <strong>{archive_name}</strong></h2>\n"
    short_body += f"<p class='da-small'><strong>Stacks:</strong> {success_count}/{stack_count} successful &nbsp;|&nbsp; <strong>Total:</strong> {size_str} &nbsp;|&nbsp; <strong>Duration:</strong> {duration_str}</p>\n"
    # concise per-stack list
    short_body += "<p>"
    short_body += ", ".join([f"{m['stack_name']} ({format_bytes(m.get('archive_size_bytes') or 0)})" for m in stack_metrics])
    short_body += "</p>\n"
    short_body += f"<p><a href=\"{base_url}/history?job={job_id}\">View details</a></p>\n"
    return short_body


def build_section_html(section_text: str) -> str:
    """Convert a section text (first line = title, rest = body) into HTML.

    section_text is plain-text with optional multiple lines. The first non-empty
    line is used as the section title and the remainder as the paragraph body.
    """
    if not section_text:
        return ''
    lines = [l for l in section_text.split('\n')]
    # Find first non-empty line as title
    title = ''
    body_lines = []
    for idx, l in enumerate(lines):
        if l.strip():
            title = l.strip()
            body_lines = lines[idx+1:]
            break
    body = '\n'.join([l for l in body_lines]).strip()
    if body:
        # Escape is handled elsewhere; this produces simple HTML
        return f"<h3>{title}</h3>\n<p>{body}</p>"
    return f"<h3>{title}</h3>"


def build_full_body(archive_name: str, status_emoji: str, success_count: int, stack_count: int, size_str: str, duration_str: str, stack_metrics: list, created_archives: list, total_size: int, reclaimed, job_log: str, base_url: str, stacks_with_volumes: list, job_id: int = None, include_log_inline: bool = True) -> str:
    """Construct the full HTML body used for rich notifications (emails / embeds).

    Parameters mirror the previous inline construction in core.py. If
    include_log_inline is False the job_log is omitted (used when the log is
    attached separately).
    """
    css = """
    <style>
    .da-table { width: 100%; border-collapse: collapse; font-family: monospace; }
    .da-table th, .da-table td { padding: 6px 8px; border-bottom: 1px solid #eee; text-align: left; }
    .da-badge-ok { color: #155724; background: #d4edda; padding: 2px 6px; border-radius: 4px; }
    .da-badge-fail { color: #721c24; background: #f8d7da; padding: 2px 6px; border-radius: 4px; }
    .da-small { font-size: 90%; color: #666; }
    </style>
    """

    body = f"{css}\n<div style='font-family: Arial, Helvetica, sans-serif; max-width:800px; margin:0; text-align:left; color:#222;'>\n  <h2 style='margin-bottom:6px;'>{status_emoji} Archive job completed: <strong>{archive_name}</strong></h2>\n  <p class='da-small'><strong>Stacks:</strong> {success_count}/{stack_count} successful &nbsp;|&nbsp; <strong>Total size:</strong> {size_str} &nbsp;|&nbsp; <strong>Duration:</strong> {duration_str}</p>\n"

    if created_archives:
        created_archives_sorted = sorted(created_archives, key=lambda x: x['path'].split('/')[-1].lower())
        body += "\n  <h3>SUMMARY OF CREATED ARCHIVES</h3>\n  <table class='da-table'>\n    <thead><tr><th style='width:110px;'>Size</th><th>Filename</th></tr></thead>\n    <tbody>\n"
        for a in created_archives_sorted:
            body += f"    <tr><td>{format_bytes(a['size'])}</td><td><code>{a['path']}</code></td></tr>\n"
        body += f"  </tbody></table>\n  <p class='da-small'><strong>Total:</strong> {format_bytes(total_size)}</p>\n"
    else:
        body += "  <p><em>No archives were created.</em></p>\n"

    try:
        disk = get_disk_usage()
        if disk and disk['total']:
            body += "\n  <h3>DISK USAGE (on /archives)</h3>\n  <p class='da-small'>\n"
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
    body += "\n  <h3>RETENTION SUMMARY</h3>\n  <p class='da-small'>\n"
    if reclaimed is None:
        body += "    No retention information available.\n"
    elif reclaimed == 0:
        body += "    No archives older than configured retention were deleted.\n"
    else:
        body += f"    Freed space: <strong>{format_bytes(reclaimed)}</strong>\n"
    body += "  </p>\n"

    # Stacks processed
    body += "\n  <h3>STACKS PROCESSED</h3>\n  <table class='da-table'>\n    <thead><tr><th>Stack</th><th>Status</th><th>Size</th><th>Archive</th></tr></thead>\n    <tbody>\n"
    for metric in stack_metrics:
        stack_name = metric['stack_name']
        status_ok = metric['status'] == 'success'
        status_html = f"<span class='{'da-badge-ok' if status_ok else 'da-badge-fail'}'>{'✓' if status_ok else '✗'}</span>"
        stack_size_str = format_bytes(metric.get('archive_size_bytes') or 0)
        archive_path = metric.get('archive_path') or ''
        body += f"    <tr><td>{stack_name}</td><td>{status_html}</td><td>{stack_size_str}</td><td><code>{archive_path or 'N/A'}</code></td></tr>\n"
    body += "  </tbody></table>\n"

    # Named volumes warning
    if stacks_with_volumes:
        body += "\n  <div style='border-top:1px solid #eee;margin:8px 0'></div>\n  <h4 style='color:orange;'>⚠️ Named Volumes Warning</h4>\n  <p class='da-small'>Named volumes are NOT included in the backup archives. Consider backing them up separately.</p>\n  <ul>\n"
        for metric in stacks_with_volumes:
            volumes = metric['named_volumes']
            body += f"    <li><strong>{metric['stack_name']}:</strong> {', '.join(volumes)}</li>\n"
        body += "  </ul>\n"

    # Full log (always expanded unless log will be attached)
    try:
        if job_log:
            if include_log_inline:
                body += "\n  <div style='border-top:1px solid #eee;margin:8px 0'></div>\n  <h3>Full job log</h3>\n  <pre style='background:#f7f7f7;padding:10px;border-radius:6px;white-space:pre-wrap;'>\n"
                body += (job_log or '') + "\n"
                body += "  </pre>\n"
            else:
                body += "\n"
    except Exception:
        pass

    # Footer
    job_ref = f"?job={job_id}" if job_id is not None else ''
    body += f"<p class='da-small'><a href=\"{base_url}/history{job_ref}\">View details</a> &nbsp;|&nbsp; Docker Archiver: <a href=\"{base_url}\">{base_url}</a></p>"

    return body


def build_sections(archive_name: str, lines: list, created_archives: list, total_size: int, stack_metrics: list, stacks_with_volumes: list, reclaimed, base_url: str, job_id: int) -> list:
    """Construct a list of plain-text sections suitable for sectioned sends (Discord embeds).

    Each section is a simple plain-text block where the first line is treated as
    the section title and the remainder as the body. This mirrors the previous
    ad-hoc section construction in `core.py` and preserves the same order.
    """
    sections = []
    # Header and summary
    sections.append('\n'.join(lines[0:2]))

    # Optional blocks
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

    # Footer is intentionally omitted here to avoid duplication in embeds —
    # `send_to_discord` will add the View details link in the final embed footer.

    return sections

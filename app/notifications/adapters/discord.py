from typing import List, Optional
from .generic import _make_apobj, _notify_with_retry
from .base import AdapterBase, AdapterResult
from app.notifications.formatters import strip_html_tags
from app.utils import get_logger

logger = get_logger(__name__)


class DiscordAdapter(AdapterBase):
    def __init__(self, webhooks: Optional[List[str]] = None):
        self.webhooks = list(webhooks or [])

    def _normalize(self, u: str) -> str:
        try:
            low = (u or '').lower()
            if low.startswith('discord://'):
                return 'https://discord.com/api/webhooks/' + u.split('://', 1)[1].lstrip('/')
            if 'discord' in low and '/webhooks/' in low:
                if low.startswith('http'):
                    return u
                else:
                    return 'https://' + u
            return u
        except Exception:
            return u

    def _sanitize_embed_options(self, embed_options: dict) -> dict:
        """Return a sanitized copy of embed_options where all names/values are strings.

        This prevents passing unexpected structures through to HTML building and
        ensures logging/serialization is deterministic for debugging.
        """
        if not embed_options:
            return {}
        out = {}
        # color: keep numeric or convert to int if possible
        color = embed_options.get('color')
        if color is not None:
            try:
                out['color'] = int(color)
            except Exception:
                try:
                    out['color'] = int(str(color), 0)
                except Exception:
                    out['color'] = str(color)
        # footer
        footer = embed_options.get('footer')
        if footer is not None:
            out['footer'] = str(footer)
        # fields
        fields = embed_options.get('fields') or []
        out_fields = []
        for f in fields:
            name = str(f.get('name')) if f and f.get('name') is not None else ''
            value = str(f.get('value')) if f and f.get('value') is not None else ''
            inline = bool(f.get('inline')) if f and 'inline' in f else False
            out_fields.append({'name': name, 'value': value, 'inline': inline})
        out['fields'] = out_fields
        return out

    def _build_html_body(self, title: str, body: str, embed_options: dict = None) -> str:
        """Construct an HTML body for Apprise/Discord that approximates an embed.
        Apprise will translate this to a suitable Discord message.

        If `body` already contains HTML tags we preserve it and inject a heading;
        otherwise we convert text to a simple HTML paragraph.
        """
        import re
        has_html = bool(re.search(r'<[^>]+>', body or ''))
        if has_html:
            desc_html = body
        else:
            desc_html = f"<p>{strip_html_tags(body)}</p>"

        html = f"<h2>{title}</h2>\n" + desc_html

        # Use sanitized embed options for building the HTML
        sopts = self._sanitize_embed_options(embed_options or {})

        fields = sopts.get('fields', [])
        if fields:
            # Use a styled divider instead of <hr> to avoid Apprise HTML->TEXT conversion edge cases
            html += "<div style='border-top:1px solid #eee;margin:8px 0'></div><table>"
            for f in fields:
                name = f.get('name', '')
                value = f.get('value', '')
                html += f"<tr><th style='text-align:left;padding-right:8px'>{name}</th><td>{value}</td></tr>"
            html += "</table>"

        footer = sopts.get('footer')
        if footer:
            html += f"<p style='color: #666;font-size:90%'>{footer}</p>"

        return html

    def send(self, title: str, body: str, body_format: object = None, attach: Optional[str] = None, context: str = '', embed_options: dict = None) -> AdapterResult:
        """Send Discord notification via Apprise. Uses HTML body to emulate embeds and passes attachments to Apprise."""
        normalized = [self._normalize(w) for w in self.webhooks]
        # Deduplicate normalized webhook URLs while preserving order so we
        # don't send the same message multiple times if a user configured
        # identical URLs more than once.
        seen = set()
        deduped = []
        for u in normalized:
            if u and u not in seen:
                seen.add(u)
                deduped.append(u)
        normalized = deduped

        # Log how many webhooks will be used — useful for diagnosing duplicate posts
        try:
            if len(normalized) > 1:
                logger.warning("DiscordAdapter: sending to %d webhooks (may cause duplicate messages if same channel used)", len(normalized))
        except Exception:
            pass

        apobj, added, err = _make_apobj(normalized)
        if apobj is None:
            logger.error("DiscordAdapter: apprise not available: %s", err)
            return AdapterResult(channel='discord', success=False, detail=err)
        if added == 0:
            return AdapterResult(channel='discord', success=False, detail='no valid discord webhook URLs added')

        html_body = self._build_html_body(title, body, embed_options=embed_options or {})

        # When attachments are provided Discord (and Apprise) will remove embeds and
        # fall back to sending the message as `content`. Discord limits `content` to
        # 2000 characters — ensure we never exceed that. If the message is too long
        # or we're attaching files, send a concise summary as content and include
        # the full details as an attachment (or let the caller attach them).
        try:
            apprise = __import__('apprise')
            NotifyFormat = getattr(apprise, 'NotifyFormat', None)
            NotifyFmtHTML = NotifyFormat.HTML if NotifyFormat is not None else None
        except Exception:
            NotifyFmtHTML = None

        # Build a plain-text version of the HTML body
        plain = strip_html_tags(html_body)

        # Convert HTML body to plain/markdown when sending embeds: Apprise's
        # Discord plugin uses NotifyFormat.MARKDOWN to build embeds. Sending
        # plain text with MARKDOWN ensures an 'embeds' payload is used instead
        # of a 'content' field which is subject to the 2000 char limit.
        try:
            apprise = __import__('apprise')
            NotifyFormat = getattr(apprise, 'NotifyFormat', None)
            NotifyFmtMarkdown = NotifyFormat.MARKDOWN if NotifyFormat is not None else None
        except Exception:
            NotifyFmtMarkdown = None

        md_body = strip_html_tags(html_body)

        # If an attachment is present, send in two steps:
        # 1) Send the embed (MARKDOWN) first without the attachment so Discord
        #    will render embeds correctly.
        # 2) Send the attachment as a separate message with a short plain-text
        #    summary (<=2000 chars) so the upload succeeds.
        if attach:
            # 1) attempt to post embeds (MARKDOWN) without attachment
            embed_ok, embed_detail = _notify_with_retry(apobj, title=title, body=md_body, body_format=NotifyFmtMarkdown, attach=None)

            # 2) prepare truncated plain content for the attachment post
            plain = md_body
            if plain and len(plain) > 2000:
                attach_body = (plain[:1950].rstrip() + '\n\n...(truncated, see attachment)') if len(plain) > 1950 else plain
                logger.warning("DiscordAdapter: attachment content too long — truncating (len=%d)", len(plain))
            else:
                attach_body = plain

            attach_ok, attach_detail = _notify_with_retry(apobj, title=title, body=attach_body, body_format=None, attach=attach)

            # Evaluate results: prefer both succeed, but consider partial success
            if embed_ok and attach_ok:
                return AdapterResult(channel='discord', success=True)

            # Partial successes: return success but log details
            if attach_ok and not embed_ok:
                logger.warning("DiscordAdapter: embeds failed but attachment succeeded (context=%s): %s", context, embed_detail)
                return AdapterResult(channel='discord', success=True, detail=f'embed failed: {embed_detail}')

            if embed_ok and not attach_ok:
                logger.warning("DiscordAdapter: embeds sent but attachment failed (context=%s): %s", context, attach_detail)
                return AdapterResult(channel='discord', success=True, detail=f'attachment failed: {attach_detail}')

            # Both failed
            detail = f'embed: {embed_detail} | attach: {attach_detail}'
            logger.error("DiscordAdapter: both embed and attachment sends failed (context=%s): %s", context, detail)
            return AdapterResult(channel='discord', success=False, detail=f'notify exception: {detail}')

        # No attachment path: send normally (MARKDOWN/Embed preferred)
        ok, detail = _notify_with_retry(apobj, title=title, body=md_body, body_format=NotifyFmtMarkdown, attach=None)

        if ok:
            return AdapterResult(channel='discord', success=True)

        # No fallback: if HTML send fails, report the original HTML send error.
        logger.error("DiscordAdapter: HTML send failed (context=%s): %s", context, detail)
        return AdapterResult(channel='discord', success=False, detail=f'notify exception: {detail}')
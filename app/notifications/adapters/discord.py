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

    def _build_md_body(self, title: str, body: str, embed_options: dict = None) -> str:
        """Construct a Markdown body for Discord embeds.

        - Uses bolded title (unless the body already starts with the title).
        - Appends sanitized fields as simple `**Name**: Value` lines.
        - Footer is **not** inlined here; footer is handled by `send_to_discord` via embed_options.
        """
        # Basic sanitization: if callers passed HTML, strip tags to a plain text fallback
        body_text = body or ''
        try:
            if '<' in body_text and '>' in body_text:
                body_text = strip_html_tags(body_text)
        except Exception:
            # If strip_html_tags fails, fall back to raw body
            pass

        # Avoid duplicating the title if body already begins with it
        try:
            body_starts_with_title = bool(body_text.strip().lower().startswith(str(title or '').strip().lower()))
        except Exception:
            body_starts_with_title = False

        if body_starts_with_title:
            md = body_text.strip()
        else:
            md = f"**{title}**\n\n{body_text.strip()}" if body_text.strip() else f"**{title}**"

        # Append fields (simple inline Markdown)
        sopts = self._sanitize_embed_options(embed_options or {})
        fields = sopts.get('fields', [])
        if fields:
            md += "\n\n"
            for f in fields:
                name = f.get('name', '')
                value = f.get('value', '')
                md += f"**{name}**: {value}\n"

        # Append footer text inline for compatibility; send_to_discord may also
        # enhance footer in the final embed footer when batching.
        footer = sopts.get('footer')
        if footer:
            md += f"\n{footer}"

        return md

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

        # Build a Markdown body directly (no HTML intermediate)
        md_body = self._build_md_body(title, body, embed_options=embed_options or {})

        # Apprise notify formats
        try:
            apprise = __import__('apprise')
            NotifyFormat = getattr(apprise, 'NotifyFormat', None)
            NotifyFmtMarkdown = NotifyFormat.MARKDOWN if NotifyFormat is not None else None
        except Exception:
            NotifyFmtMarkdown = None

        # If an attachment is present, send in two steps:
        # 1) Send the embed (MARKDOWN) first without the attachment so Discord
        #    will render embeds correctly.
        # 2) Send the attachment as a separate message with a short plain-text
        #    summary (<=2000 chars) so the upload succeeds.
        if attach:
            # 1) attempt to post embeds (MARKDOWN) without attachment
            try:
                embed_ok, embed_detail = _notify_with_retry(apobj, title=title, body=md_body, body_format=NotifyFmtMarkdown, attach=None, capture_logs_on_success=True)
            except TypeError:
                # Older test fakes may not accept the new keyword; fall back gracefully
                embed_ok, embed_detail = _notify_with_retry(apobj, title=title, body=md_body, body_format=NotifyFmtMarkdown, attach=None)
            if embed_detail:
                # Log captured Apprise debug output to help diagnose internal splitting
                logger.debug("DiscordAdapter: apprise logs on embed send: %s", embed_detail)
                # Count how many Discord POSTs Apprise performed in this call
                try:
                    num_posts = embed_detail.count('Discord POST URL:') + embed_detail.count('Discord POST URL')
                    # Fallback: count Discord Payload sections
                    if num_posts == 0:
                        num_posts = embed_detail.count('Discord Payload:')
                    if num_posts > 1:
                        logger.warning("DiscordAdapter: Apprise performed %d POSTs for a single notify() call (may produce duplicate messages).", num_posts)
                except Exception:
                    pass

            # 2) prepare truncated plain content for the attachment post
            plain = md_body
            if plain and len(plain) > 2000:
                attach_body = (plain[:1950].rstrip() + '\n\n...(truncated, see attachment)') if len(plain) > 1950 else plain
                logger.warning("DiscordAdapter: attachment content too long — truncating (len=%d)", len(plain))
            else:
                attach_body = plain

            try:
                attach_ok, attach_detail = _notify_with_retry(apobj, title=title, body=attach_body, body_format=None, attach=attach, capture_logs_on_success=True)
            except TypeError:
                attach_ok, attach_detail = _notify_with_retry(apobj, title=title, body=attach_body, body_format=None, attach=attach)
            if attach_detail:
                logger.debug("DiscordAdapter: apprise logs on attach send: %s", attach_detail)

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

        if detail:
            logger.debug("DiscordAdapter: apprise logs on normal send: %s", detail)
            try:
                num_posts = detail.count('Discord POST URL:') + detail.count('Discord POST URL')
                if num_posts == 0:
                    num_posts = detail.count('Discord Payload:')
                if num_posts > 1:
                    logger.warning("DiscordAdapter: Apprise performed %d POSTs for a single notify() call (may produce duplicate messages).", num_posts)
            except Exception:
                pass

        if ok:
            return AdapterResult(channel='discord', success=True)

        # No fallback: if send fails, report the original send error.
        logger.error("DiscordAdapter: notify failed (context=%s): %s", context, detail)
        return AdapterResult(channel='discord', success=False, detail=f'notify exception: {detail}')
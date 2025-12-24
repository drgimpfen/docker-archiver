"""Centralized helpers to send sectioned/embed Discord notifications.

This module provides a single entry point `send_to_discord` which handles
splitting long messages into sections, building simple HTML for each section
and calling a `DiscordAdapter` to deliver each part. It intentionally keeps
dependencies minimal so it can be unit tested with a fake adapter.
"""
from typing import List, Optional
import time
import copy
from app.utils import get_logger
from .formatters import split_section_by_length, build_section_html

logger = get_logger(__name__)


def send_to_discord(discord_adapter, title: str, body_html: str, compact_text: str, sections: List[str], attach_file: Optional[str] = None, embed_options: dict = None, max_desc: int = 4000, pause: float = 0.2, view_url: Optional[str] = None) -> dict:
    """Send a rich (HTML) message to Discord using the provided adapter.

    Behavior:
    - If the compact_text fits within `max_desc` a single message is sent with
      the provided full `body_html` so Apprise can render a single embed.
    - Otherwise the message is split into `sections` and each section is
      split via `split_section_by_length` and sent individually. The attachment
      (if provided) is included only on the final section's final part.
    - `embed_options` are applied to each send; the footer is removed on
      intermediate parts to avoid repetition.

    Returns a dictionary with keys:
    - sent_any: bool — whether at least one send returned success
    - details: optional message or list of errors
    """
    if discord_adapter is None:
        return {'sent_any': False, 'details': 'no adapter'}

    sent_any = False
    errors = []

    try:
        # Build markdown body from sections (prefer a single Markdown send following borg-ui style)
        md_parts = []
        for sec in sections:
            first, rest = sec.split('\n', 1) if '\n' in sec else (sec, '')
            # Use bold titles instead of H2 headings to avoid Apprise splitting into multiple embeds
            md_parts.append(f"**{first}**\n{rest}")
        md_body = '\n\n'.join(md_parts)

        # Remove duplicated title if present at the start of the body (prevents
        # the subject appearing twice because Apprise also prepends the title)
        try:
            if title:
                tclean = title.strip()
                tclean_low = tclean.lower()
                mb_low = md_body.lower()
                # Patterns to remove at body start: bold title, plain title, title with bracket
                for pat in (f"**{tclean}**", tclean, f"[{tclean}]"):
                    if mb_low.startswith(pat.lower()):
                        # strip the pattern from start
                        md_body = md_body[len(pat):].lstrip('\n\r ') if len(md_body) > len(pat) else ''
                        break
        except Exception:
            pass

        # Logging for debugging: sizes and section summaries
        try:
            logger.info("send_to_discord: md_body_len=%d sections=%d", len(md_body or ''), len(sections or []))
            for idx, sec in enumerate(sections or []):
                s_title = sec.split('\n', 1)[0] if sec else ''
                s_len = len(sec or '')
                logger.debug("send_to_discord: section %d title=%r len=%d", idx + 1, s_title, s_len)
        except Exception:
            pass
        # Prefer a single Markdown embed (borg-ui style). If content exceeds the per-embed limit,
        # pack sections into as few embeds as possible (no truncation).
        effective_limit = min(max_desc, 1800)
        try:
            # If it fits in a single embed, send it
            if md_body and len(md_body) <= effective_limit:
                if attach_file:
                    # two-step attach flow for small single body
                    res_embed = discord_adapter.send(title, md_body, body_format=__import__('apprise').NotifyFormat.MARKDOWN, attach=None, context='discord_single', embed_options=embed_options)
                    try:
                        from app.notifications.formatters import strip_html_tags
                        attach_body = strip_html_tags(md_body)
                    except Exception:
                        attach_body = compact_text or ''
                    if view_url:
                        attach_body = f"{attach_body}\n\nView details: {view_url}"
                    if len(attach_body) > 2000:
                        attach_body = attach_body[:1950].rstrip() + '\n\n...(truncated, see attachment)'
                    res_attach = discord_adapter.send(title, attach_body, body_format=None, attach=attach_file, context='discord_attach')
                    if res_embed.success and res_attach.success:
                        return {'sent_any': True}
                    details = []
                    if not res_embed.success:
                        details.append(f"embed: {res_embed.detail}")
                    if not res_attach.success:
                        details.append(f"attach: {res_attach.detail}")
                    if res_embed.success or res_attach.success:
                        return {'sent_any': True, 'details': details}
                else:
                    res = discord_adapter.send(title, md_body, body_format=__import__('apprise').NotifyFormat.MARKDOWN, attach=None, context='discord_single', embed_options=embed_options)
                    if res.success:
                        return {'sent_any': True}
                    logger.error("send_to_discord: single send failed - detail=%s", res.detail)
        except Exception as e:
            logger.exception("send_to_discord: exception during single send (title=%s): %s -- body_type=%s embed_type=%s", title, e, type(body_html), type(embed_options))
            # Fall back to batching below
            pass

        # Otherwise send sectioned messages
        # Build parts for all sections first (so we can batch them into larger embeds)
        # Use a conservative per-batch limit to avoid Apprise/Discord internally
        # splitting embeds into multiple posts (Discord description/field limits).
        effective_max_desc = min(max_desc, 1800)
        per_part_limit = min(effective_max_desc, 1000)
        parts = []
        for sec in sections:
            sec_title = (sec.split('\n', 1)[0] or title)[:250]
            body_part = sec.split('\n', 1)[1] if '\n' in sec else ''
            sec_parts = split_section_by_length(body_part, per_part_limit)
            for i, part in enumerate(sec_parts):
                parts.append({'title': sec_title, 'part': part, 'is_first': i == 0})

        # Batch parts into embeddable groups respecting max_desc and a batch size by unique sections
        batch_size = 10  # max unique sections per embed
        batches = []
        cur = []
        cur_len = 0
        cur_sections = set()
        for p in parts:
            formatted = f"## {p['title']}\n{p['part']}"
            extra_section = 1 if (p['is_first'] and p['title'] not in cur_sections) else 0
            next_len = cur_len + (2 if cur_len else 0) + len(formatted)
            if cur and (next_len > max_desc or len(cur_sections) + extra_section > batch_size):
                batches.append(cur)
                cur = []
                cur_len = 0
                cur_sections = set()
            # append
            cur.append((p['title'], formatted, p['is_first']))
            cur_len += (2 if cur_len else 0) + len(formatted)
            if p['is_first']:
                cur_sections.add(p['title'])
        if cur:
            batches.append(cur)

        # Send each batch as a single embed
        logger.info("send_to_discord: sending %d batches (effective_max_desc=%d)", len(batches), effective_max_desc)
        for bidx, batch in enumerate(batches):
            is_final_batch = (bidx == len(batches) - 1)
            # build batch markdown
            md_body = '\n\n'.join(item[1] for item in batch)
            # Log batch summary for debugging (length and prefix)
            try:
                snippet = md_body[:200].replace('\n', ' ')
                logger.debug("send_to_discord: batch %d/%d length=%d prefix=%s", bidx + 1, len(batches), len(md_body), snippet)
            except Exception:
                pass

            sec_embed_opts = copy.deepcopy(embed_options or {})
            if not is_final_batch and 'footer' in sec_embed_opts:
                sec_embed_opts.pop('footer', None)
            if is_final_batch and view_url:
                footer = sec_embed_opts.get('footer')
                if footer:
                    sec_embed_opts['footer'] = f"{footer} | View details: {view_url}"
                else:
                    sec_embed_opts['footer'] = f"View details: {view_url}"

            try:
                res = discord_adapter.send(title, md_body, body_format=__import__('apprise').NotifyFormat.MARKDOWN, attach=None, context='discord_section', embed_options=sec_embed_opts)
            except Exception as e:
                logger.exception("send_to_discord: exception during batch send (title=%s): %s", title, e)
                errors.append(str(e))
                continue

            if res.success:
                sent_any = True
                logger.debug("send_to_discord: batch %d sent successfully", bidx + 1)
            else:
                logger.error("send_to_discord: batch send failed - detail=%s", res.detail)
                errors.append(res.detail)

            if pause:
                time.sleep(pause)

        # After sections: if an attachment is provided, send it as a single follow-up message
        if attach_file:
            try:
                # Use the stripped HTML as the attach message content if available
                try:
                    from app.notifications.formatters import strip_html_tags
                    attach_body = strip_html_tags(body_html)
                except Exception:
                    attach_body = compact_text or ''
                if view_url:
                    attach_body = f"{attach_body}\n\nView details: {view_url}"
                if len(attach_body) > 2000:
                    attach_body = attach_body[:1950].rstrip() + '\n\n...(truncated, see attachment)'

                res_attach = discord_adapter.send(title, attach_body, body_format=None, attach=attach_file, context='discord_attach')
                if res_attach.success:
                    sent_any = True
                else:
                    errors.append(res_attach.detail)
            except Exception as e:
                logger.exception("send_to_discord: exception during attach send: %s", e)
                errors.append(str(e))

        if sent_any:
            return {'sent_any': True}
        return {'sent_any': False, 'details': errors}

    except Exception as e:
        logger.exception("send_to_discord: unexpected error: %s", e)
        return {'sent_any': False, 'details': str(e)}

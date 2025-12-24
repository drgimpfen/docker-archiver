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

    def _build_html_body(self, title: str, body: str, embed_options: dict = None) -> str:
        """Construct an HTML body for Apprise/Discord that approximates an embed.
        Apprise will translate this to a suitable Discord message."""
        # Convert HTML to safe text and then build a simple HTML structure
        desc = strip_html_tags(body)
        html = f"<h2>{title}</h2>\n<p>{desc}</p>"
        if embed_options:
            # fields
            fields = embed_options.get('fields', [])
            if fields:
                html += "<hr><table>"
                for f in fields:
                    name = f.get('name')
                    value = f.get('value')
                    html += f"<tr><th style='text-align:left;padding-right:8px'>{name}</th><td>{value}</td></tr>"
                html += "</table>"
            # footer
            footer = embed_options.get('footer')
            if footer:
                html += f"<p style='color: #666;font-size:90%'>" + str(footer) + "</p>"
        return html

    def send(self, title: str, body: str, body_format: object = None, attach: Optional[str] = None, context: str = '', embed_options: dict = None) -> AdapterResult:
        """Send Discord notification via Apprise. Uses HTML body to emulate embeds and passes attachments to Apprise."""
        normalized = [self._normalize(w) for w in self.webhooks]
        apobj, added, err = _make_apobj(normalized)
        if apobj is None:
            logger.error("DiscordAdapter: apprise not available: %s", err)
            return AdapterResult(channel='discord', success=False, detail=err)
        if added == 0:
            return AdapterResult(channel='discord', success=False, detail='no valid discord webhook URLs added')

        html_body = self._build_html_body(title, body, embed_options=embed_options or {})

        ok, detail = _notify_with_retry(apobj, title=title, body=html_body, body_format=__import__('apprise').NotifyFormat.HTML, attach=attach)
        if ok:
            return AdapterResult(channel='discord', success=True)
        return AdapterResult(channel='discord', success=False, detail=f'notify exception: {detail}')
"""SMTP adapter for sending HTML emails using environment SMTP settings."""
from typing import Optional, List
import os
import smtplib
from email.message import EmailMessage
from app.notifications.adapters.base import AdapterBase, AdapterResult
from app.notifications.core import get_subject_with_tag, get_user_emails
from app.notifications.formatters import strip_html_tags
from app.utils import get_logger

logger = get_logger(__name__)


class SMTPAdapter(AdapterBase):
    def __init__(self):
        # Read configuration from DB settings via get_setting() (preferred).
        # Fall back to environment variables if get_setting is unavailable for safety.
        # Read configuration from DB settings via get_setting (preferred).
        # Do NOT fall back to environment variables anymore - SMTP is configured via application settings.
        try:
            from app.notifications.core import get_setting
            self.server = get_setting('smtp_server', None) or None
            port = get_setting('smtp_port', '') or ''
            try:
                self.port = int(port) if port else 587
            except Exception:
                self.port = 587
            self.user = get_setting('smtp_user', None) or None
            self.password = get_setting('smtp_password', None) or None
            self.from_addr = get_setting('smtp_from', None) or None
            self.use_tls = str(get_setting('smtp_use_tls', 'true')).lower() in ('1', 'true', 'yes')
        except Exception:
            # If settings cannot be read, leave SMTP configuration unset
            self.server = None
            self.port = 587
            self.user = None
            self.password = None
            self.from_addr = None
            self.use_tls = True

    def _get_recipients(self, recipients: Optional[List[str]] = None) -> List[str]:
        if recipients and len(recipients) > 0:
            return recipients
        # Fall back to user emails from profile (import at runtime so tests can monkeypatch)
        try:
            from app.notifications.core import get_user_emails as _get_user_emails
            return _get_user_emails()
        except Exception:
            return []

    def send(self, title: str, body: str, body_format: object = None, attach: Optional[str] = None, recipients: Optional[List[str]] = None, context: str = '') -> AdapterResult:
        if not self.server or not self.from_addr:
            return AdapterResult(channel='smtp', success=False, detail='SMTP server or from address not configured')

        to_addrs = self._get_recipients(recipients)
        if not to_addrs:
            return AdapterResult(channel='smtp', success=False, detail='no recipients')

        # Build EmailMessage
        msg = EmailMessage()
        msg['Subject'] = title
        msg['From'] = self.from_addr
        msg['To'] = ', '.join(to_addrs)

        # Always send HTML; include text alternative
        html_body = body or ''
        text_body = strip_html_tags(html_body)
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype='html')

        # Attach file if provided
        if attach:
            try:
                with open(attach, 'rb') as f:
                    data = f.read()
                import mimetypes
                ctype, encoding = mimetypes.guess_type(attach)
                if ctype:
                    maintype, subtype = ctype.split('/', 1)
                else:
                    maintype, subtype = 'application', 'octet-stream'
                msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=attach.split('/')[-1])
            except Exception as e:
                logger.exception('SMTPAdapter: failed to attach file: %s', e)
                return AdapterResult(channel='smtp', success=False, detail=f'attach error: {e}')

        # Send via SMTP
        try:
            if self.use_tls:
                smtp = smtplib.SMTP(self.server, self.port, timeout=10)
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            else:
                smtp = smtplib.SMTP(self.server, self.port, timeout=10)

            try:
                if self.user and self.password:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
            finally:
                try:
                    smtp.quit()
                except Exception:
                    pass

            return AdapterResult(channel='smtp', success=True)
        except Exception as e:
            logger.exception('SMTPAdapter: failed to send email: %s', e)
            return AdapterResult(channel='smtp', success=False, detail=str(e))

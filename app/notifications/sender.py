"""
SMTP sender module for notifications.
"""
import os
from app.notifications.helpers import get_setting, get_user_emails
from app.utils import get_logger

logger = get_logger(__name__)


def send_email(title, body, attach=None, recipients=None, context=''):
    """
    Send an email via SMTP with optional attachment.
    
    Args:
        title: Email subject
        body: Email body (HTML)
        attach: Path to attachment file (optional)
        recipients: List of email addresses (optional, defaults to user emails)
        context: Context string for logging
    
    Returns:
        bool: True if successful
    """
    if recipients is None:
        recipients = get_user_emails() or []
    
    if not recipients:
        logger.warning("No recipients configured for notification. context=%s", context)
        return False
    
    try:
        from app.notifications.adapters import SMTPAdapter
        smtp_adapter = SMTPAdapter() if get_setting('smtp_server') else None
        if not smtp_adapter:
            logger.warning("SMTP not configured; skipping email. context=%s", context)
            return False
        
        res = smtp_adapter.send(title, body, body_format=None, attach=attach, recipients=recipients, context=context)
        if res.success:
            logger.info("SMTP email sent successfully. context=%s", context)
            return True
        else:
            logger.error("SMTP send failed: %s. context=%s", res.detail, context)
            return False
    except Exception as e:
        logger.exception("Error sending email: %s. context=%s", e, context)
        return False
    finally:
        # Clean up attachment if it was created
        if attach and os.path.exists(attach):
            try:
                os.unlink(attach)
            except Exception:
                pass
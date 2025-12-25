"""Small helper utilities used by the notification subsystem.

This module contains pure helpers (settings lookup, user email resolution, subject tag
helpers and notification enablement checks). Having these small functions in their
own module makes them easy to test and to reuse from other notification modules.
"""
from typing import List
from app.db import get_db
from app.utils import get_logger

logger = get_logger(__name__)


def get_setting(key: str, default: str = '') -> str:
    """Return a setting value from the database (best-effort).

    This is intentionally forgiving: on any error it returns the provided default.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = %s;", (key,))
            result = cur.fetchone()
            return result['value'] if result else default
    except Exception:
        return default


def get_user_emails() -> List[str]:
    """Return all configured user emails (best-effort).

    Returns an empty list on error.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT email FROM users WHERE email IS NOT NULL AND email != '';")
            results = cur.fetchall()
            return [row['email'] for row in results]
    except Exception:
        return []


def get_subject_with_tag(subject: str) -> str:
    """Prefix the notification subject with an optional tag from settings.

    E.g., if setting notification_subject_tag is "[DA]", then
    get_subject_with_tag('Hello') -> '[DA] Hello'
    """
    tag = get_setting('notification_subject_tag', '').strip()
    if tag:
        return f"{tag} {subject}"
    return subject


def get_notification_format() -> str:
    """Return preferred notification format (currently always 'html')."""
    return 'html'


def should_notify(event_type: str) -> bool:
    """Return whether notifications are enabled for the event type.

    The setting keys expected are `notify_on_<event_type>`, stored as 'true'/'false'.
    """
    key = f"notify_on_{event_type}"
    value = get_setting(key, 'false')
    return value.lower() == 'true'

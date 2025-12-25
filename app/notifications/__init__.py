"""Package shim for notifications.

This module re-exports the public symbols from `core.py` so existing imports
such as `from app.notifications import send_archive_notification` continue to
work while the codebase is migrated to a package layout.
"""

from .helpers import *
from .formatters import strip_html_tags, build_compact_text, split_section_by_length
from .core import *  # keep core notifications available

__all__ = [
    name for name in dir() if not name.startswith('_')
]

"""Package shim for notifications.

This module re-exports the public symbols from submodules so existing imports
such as `from app.notifications import send_archive_notification` continue to
work while the codebase is migrated to a package layout.
"""

from .helpers import *
from .formatters import strip_html_tags, build_compact_text, split_section_by_length
from .handlers import *
from .sender import send_email

__all__ = [
    name for name in dir() if not name.startswith('_')
]

"""
Utility functions for the application.
"""
import os
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

# Central logging helpers
def setup_logging():
    """Configure root logger from environment LOG_LEVEL.

    Intended to be called early during application startup (e.g., from main.py).
    Uses LOG_LEVEL env var (e.g., DEBUG, INFO, WARNING, ERROR); defaults to INFO.
    """
    level_name = os.environ.get('LOG_LEVEL', 'INFO').upper()
    try:
        level = getattr(logging, level_name)
    except Exception:
        level = logging.INFO
    # Only configure basicConfig if no handlers are present so tests or other
    # environments can configure logging differently
    if not logging.getLogger().handlers:
        logging.basicConfig(level=level, format='[%(levelname)s] %(asctime)s %(name)s: %(message)s')
    logging.getLogger().setLevel(level)


def get_logger(name=None):
    """Return a logger for the given name (or the module logger if none)."""
    return logging.getLogger(name if name else __name__)



def now():
    """Get current datetime in UTC (for database storage).

    Returns a timezone-aware datetime with tzinfo=timezone.utc to avoid naive/aware
    mismatches across the app."""
    from datetime import timezone
    return datetime.now(timezone.utc)


def local_now():
    """Get current datetime in local timezone (for filenames, logs).

    Returns a timezone-aware datetime in the configured display timezone."""
    tz = get_display_timezone()
    from datetime import timezone
    return datetime.now(timezone.utc).astimezone(tz)


def get_display_timezone():
    """Get the configured display timezone."""
    tz_name = os.environ.get('TZ', 'UTC')
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo('UTC')


def format_datetime(dt, format_string='%Y-%m-%d %H:%M:%S'):
    """Convert a datetime (assumed UTC if naive) to local timezone for display.

    - If `dt` is naive, it's assumed to be UTC and is made timezone-aware.
    - If `dt` is timezone-aware, it's converted from its timezone to the display timezone.
    """
    if dt is None:
        return '-'
    if not isinstance(dt, datetime):
        return str(dt)

    from datetime import timezone
    local_tz = get_display_timezone()

    try:
        if getattr(dt, 'tzinfo', None):
            # dt is timezone-aware: convert to display tz
            dt_local = dt.astimezone(local_tz)
        else:
            # dt is naive: assume it's in the system/display timezone (configured via TZ)
            sys_tz = get_display_timezone()
            dt_local = dt.replace(tzinfo=sys_tz).astimezone(local_tz)
    except Exception:
        # Fallback: try treating as UTC
        try:
            dt_local = dt.replace(tzinfo=timezone.utc).astimezone(local_tz)
        except Exception:
            return str(dt)

    return dt_local.strftime(format_string)


def format_bytes(bytes_val):
    """Format bytes to human readable string."""
    if bytes_val is None:
        return 'N/A'
    
    # Convert to float to handle Decimal from database
    bytes_val = float(bytes_val)
    
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f}{unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f}PB"


def format_duration(seconds):
    """Format duration in seconds to human readable string."""
    if seconds is None:
        return 'N/A'
    
    if seconds < 60:
        return f"{seconds}s"
    
    minutes = seconds // 60
    secs = seconds % 60
    
    if minutes < 60:
        return f"{minutes}m {secs}s"
    
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"


def get_disk_usage(path='/archives'):
    """
    Get disk usage for specified directory.
    
    Returns dict with total, used, free (bytes) and percent.
    """
    try:
        usage = shutil.disk_usage(path)
        return {
            'total': usage.total,
            'used': usage.used,
            'free': usage.free,
            'percent': (usage.used / usage.total) * 100
        }
    except Exception:
        return {'total': 0, 'used': 0, 'free': 0, 'percent': 0}


def to_iso_z(dt):
    """Convert a datetime-like object to an ISO 8601 UTC string ending with 'Z'.

    If `dt` is None, returns None. If `dt` isn't a datetime-like object, returns
    the original value as string.
    """
    if dt is None:
        return None
    try:
        from datetime import timezone
        if hasattr(dt, 'astimezone'):
            return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        pass
    return str(dt)


def filename_safe(name):
    """Return a filesystem-safe name derived from the provided string.

    Replaces any character not in [A-Za-z0-9_-] with underscore and collapses
    repeated underscores.
    """
    try:
        import re
        safe = re.sub(r'[^A-Za-z0-9_-]+', '_', str(name))
        safe = re.sub(r'_+', '_', safe).strip('_')
        return safe or 'unnamed'
    except Exception:
        return 'unnamed'

"""
Utility functions for the application.
"""
import os
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo


def now():
    """Get current datetime in UTC (for database storage)."""
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_now():
    """Get current datetime in local timezone (for filenames, logs)."""
    tz = get_display_timezone()
    from datetime import timezone
    return datetime.now(timezone.utc).astimezone(tz).replace(tzinfo=None)


def get_display_timezone():
    """Get the configured display timezone."""
    tz_name = os.environ.get('TZ', 'UTC')
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo('UTC')


def format_datetime(dt, format_string='%Y-%m-%d %H:%M:%S'):
    """Convert UTC datetime to local timezone for display."""
    if dt is None:
        return '-'
    if not isinstance(dt, datetime):
        return str(dt)
    
    # Assume dt is UTC (from database)
    from datetime import timezone
    dt_utc = dt.replace(tzinfo=timezone.utc)
    
    # Convert to display timezone
    local_tz = get_display_timezone()
    dt_local = dt_utc.astimezone(local_tz)
    
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

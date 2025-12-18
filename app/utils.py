"""
Utility functions for the application.
"""
import shutil


def format_bytes(bytes_val):
    """Format bytes to human readable string."""
    if bytes_val is None:
        return 'N/A'
    
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

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
    """Configure root logger from environment.

    - Uses LOG_LEVEL env var (e.g., DEBUG, INFO); defaults to INFO.
    - If no handlers exist, installs a StreamHandler and a TimedRotatingFileHandler
      to write daily log files into the fixed JOBS_LOG_DIR.

    Note: File logging is always enabled. The jobs log directory and filename are
    fixed to `JOBS_LOG_DIR` and `LOG_FILE_NAME` respectively; rotation backup
    counts are not used because retention is managed by the cleanup job.
    - JOBS_LOG_DIR: '/var/log/archiver'
    - LOG_FILE_NAME: 'app.log'
    """
    level_name = os.environ.get('LOG_LEVEL', 'INFO').upper()
    try:
        level = getattr(logging, level_name)
    except Exception:
        level = logging.INFO

    root = logging.getLogger()

    # Only configure handlers if none are present so tests or other
    # environments can configure logging differently
    if not root.handlers:
        # Stream handler (stdout/stderr -> container logs)
        sh = logging.StreamHandler()
        sh.setLevel(level)
        sh.setFormatter(logging.Formatter('[%(levelname)s] %(asctime)s %(name)s: %(message)s'))
        root.addHandler(sh)

        # Always add a daily rotating file handler; paths and filename are fixed.
        log_dir = get_log_dir()
        log_file_name = LOG_FILE_NAME
        try:
            os.makedirs(log_dir, exist_ok=True)
            from logging.handlers import TimedRotatingFileHandler
            fh = TimedRotatingFileHandler(
                filename=os.path.join(log_dir, log_file_name),
                when='midnight',
                backupCount=0,
                encoding='utf-8'
            )
            fh.setLevel(level)
            fh.setFormatter(logging.Formatter('[%(levelname)s] %(asctime)s %(name)s: %(message)s'))
            root.addHandler(fh)
        except Exception as e:
            # If file logging cannot be set up, log a warning to the stream handler
            root.warning("Failed to configure file logging (LOG_DIR=%s): %s", log_dir, e)

    root.setLevel(level)


def get_logger(name=None):
    """Return a logger for the given name (or the module logger if none)."""
    return logging.getLogger(name if name else __name__)


# Helpers for per-job logging
class StreamToLogger:
    """File-like object that redirects writes to a logger instance.

    Usage:
        job_logger, handler = get_job_logger(archive_id, archive_name)
        sys.stdout = StreamToLogger(job_logger, level=logging.INFO)
        sys.stderr = StreamToLogger(job_logger, level=logging.ERROR)
    """
    def __init__(self, logger, level=logging.INFO):
        self.logger = logger
        self.level = level
        self._buf = ''

    def write(self, buf):
        try:
            if not buf:
                return
            self._buf += str(buf)
            while '\n' in self._buf:
                line, self._buf = self._buf.split('\n', 1)
                if line:
                    self.logger.log(self.level, line)
        except Exception:
            pass

    def flush(self):
        if self._buf:
            try:
                self.logger.log(self.level, self._buf)
            except Exception:
                pass
            self._buf = ''


def get_job_logger(archive_id, archive_name, log_path: str | None = None, level=logging.INFO):
    """Create or return a per-archive job logger.

    - Default log path: <LOG_DIR>/jobs/archive_{archive_id}_{archive_name}_{UTC_TIMESTAMP}.log
    - Returns (logger, handler) where handler may be None if an existing handler
      for the same path was already attached.
    """
    import os
    from logging.handlers import TimedRotatingFileHandler

    safe_name = filename_safe(archive_name)
    jobs_dir = os.path.join(get_log_dir(), 'jobs')
    try:
        os.makedirs(jobs_dir, exist_ok=True)
    except Exception:
        pass

    if not log_path:
        ts = filename_timestamp()
        log_path = os.path.join(jobs_dir, f"archive_{archive_id}_{safe_name}_{ts}.log")

    logger = logging.getLogger(f"job.{archive_id}_{safe_name}")
    logger.setLevel(level)

    # Check if a handler with the same filename is already present
    abspath = os.path.abspath(log_path)
    for h in list(logger.handlers):
        try:
            if getattr(h, 'baseFilename', None) == abspath:
                return logger, None
        except Exception:
            continue

    try:
        handler = TimedRotatingFileHandler(filename=log_path, when='midnight', backupCount=0, encoding='utf-8')
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(asctime)s %(name)s: %(message)s'))
        logger.addHandler(handler)
        return logger, handler
    except Exception:
        # If handler creation fails, return logger with no handler (caller should fallback)
        return logger, None


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


# Fixed paths used across the application. These are centralized so they can be
# adjusted in one place if needed. They are intentionally NOT controlled via
# environment variables to avoid accidental misconfiguration at runtime.
ARCHIVES_PATH = '/archives'
LOG_FILE_NAME = 'app.log'
LOG_DIR = '/var/log/archiver'
DOWNLOADS_PATH = '/tmp/downloads'
SENTINEL_DIR = '/tmp'  


def get_archives_path():
    """Return the canonical archives directory used by the application."""
    return ARCHIVES_PATH


def get_log_dir():
    """Return the canonical log directory used by the application.

    The log directory is fixed to the constant `LOG_DIR` to avoid runtime
    misconfiguration at runtime.
    """
    return LOG_DIR


def get_downloads_path():
    """Return the canonical temporary downloads directory used by the application (Path object)."""
    from pathlib import Path
    return Path(DOWNLOADS_PATH)


def get_sentinel_path(name: str):
    """Return a sentinel filename under the sentinel directory for the given name."""
    import os
    return os.path.join(SENTINEL_DIR, name)


def get_disk_usage(path=None):
    """
    Get disk usage for specified directory. If no path is provided, use the
    canonical archives path.

    Returns dict with total, used, free (bytes) and percent.
    """
    if path is None:
        path = get_archives_path()
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


def filename_timestamp(dt=None):
    """Return a timestamp string suitable for filenames using configured local timezone.

    Format: YYYYMMDD_HHMMSS (e.g. 20251225_182530) using `local_now()` so the timestamp
    reflects the configured display timezone (TZ).
    If `dt` is None, uses current local time via `local_now()` helper.
    """
    try:
        if dt is None:
            dt = local_now()
        # Ensure dt is a datetime and format using local timezone
        return dt.strftime('%Y%m%d_%H%M%S')
    except Exception:
        # Fallback to epoch seconds if anything goes wrong
        import time
        return time.strftime('%Y%m%d_%H%M%S', time.localtime())


def ensure_utc(dt):
    """Normalize a datetime-like object to a timezone-aware UTC datetime.

    - If `dt` is ``None``, returns ``None``.
    - If `dt` is naive (no tzinfo), it's assumed to be UTC and ``tzinfo`` is set
      to ``timezone.utc``.
    - If `dt` is timezone-aware, it is converted to UTC.
    """
    if dt is None:
        return None
    try:
        from datetime import timezone
        if getattr(dt, 'tzinfo', None) is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return dt


def filename_safe(name):
    """Return a filesystem-safe name derived from the provided string.
"},{ 
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


def make_download_filename(original_name: str) -> str:
    """Normalize an original filename for download storage.

    - Strips repeated leading timestamp+download prefixes like
      `20251224_181226_download_20251224_181032_download_...`.
    - Collapses repeated extension segments (e.g. `.zst.zst` -> `.zst`).
    - Normalizes patterns like `_tar_zst` to `.tar.zst`.
    - Returns a filename consisting of a filesystem-safe stem and normalized suffixes.
    """
    try:
        import re
        from pathlib import Path

        name = Path(str(original_name)).name
        # strip repeated leading timestamp download prefixes
        name = re.sub(r'^(?:\d{8}_\d{6}_download_)+', '', name)
        # collapse duplicate underscores
        name = re.sub(r'_+', '_', name)
        # convert patterns like _tar_zst or _tar_gz to .tar.zst / .tar.gz
        name = re.sub(r'(_tar_zst)+$', '.tar.zst', name, flags=re.IGNORECASE)
        name = re.sub(r'(_tar_gz)+$', '.tar.gz', name, flags=re.IGNORECASE)
        # collapse repeated dot-extensions like .zst.zst -> .zst
        name = re.sub(r'(?:\.zst)+$', '.zst', name, flags=re.IGNORECASE)
        name = re.sub(r'(?:\.gz)+$', '.gz', name, flags=re.IGNORECASE)
        # ensure .tar.zst duplicates are collapsed
        name = re.sub(r'\.tar\.zst(?:\.zst)+$', '.tar.zst', name, flags=re.IGNORECASE)
        name = re.sub(r'\.tar\.gz(?:\.gz)+$', '.tar.gz', name, flags=re.IGNORECASE)

        p = Path(name)
        suffixes = ''.join(p.suffixes)
        if suffixes:
            stem = p.name[:-len(suffixes)]
        else:
            stem = p.stem

        safe_stem = filename_safe(stem)
        return f"{safe_stem}{suffixes}"
    except Exception:
        return filename_safe(original_name)


def unique_filename(target_dir: str, filename: str) -> str:
    """Return a filename that does not conflict in target_dir by appending _1, _2, ... if needed."""
    from pathlib import Path
    try:
        td = Path(target_dir)
        candidate = td / filename
        if not candidate.exists():
            return filename
        stem = candidate.stem
        suffix = ''.join(candidate.suffixes)
        i = 1
        while True:
            new_name = f"{stem}_{i}{suffix}"
            if not (td / new_name).exists():
                return new_name
            i += 1
    except Exception:
        return filename


def apply_permissions_recursive(base_path, file_mode=0o644, dir_mode=0o755, collect_list=False, report_path=None):
    """Recursively apply permissions to files and directories under base_path.

    Returns a dict with counts: {'files_changed': int, 'dirs_changed': int, 'errors': int}.
    If collect_list is True, also returns 'fixed_files' and 'fixed_dirs' lists (limited by an internal cap).
    If `report_path` is provided, a full report (one entry per fixed path) is appended to the file
    as the job runs (format: 'F\t<path>' for files, 'D\t<path>' for directories).

    Note: the `max_samples` public parameter was removed to simplify the API. An
    internal safe cap (`_MAX_SAMPLES`) protects memory usage when `collect_list` is True.
    This is a best-effort operation and will continue on errors.
    """
    import os
    from pathlib import Path

    files_changed = 0
    dirs_changed = 0
    errors = 0
    fixed_files = []
    fixed_dirs = []
    report_file = None

    # Internal cap for in-memory samples to avoid memory blowup when collect_list is True
    _MAX_SAMPLES = 1000

    logger = get_logger(__name__)
    try:
        if report_path:
            try:
                report_file = open(report_path, 'a', encoding='utf-8')
                report_file.write(f"# Permissions fix report for base: {base_path}\n")
                report_file.write(f"# Started: {datetime.utcnow().isoformat()}Z\n")
                try:
                    report_file.flush()
                    try:
                        os.fsync(report_file.fileno())
                    except Exception:
                        # fsync is best-effort; ignore if not supported
                        pass
                except Exception:
                    logger.debug("Failed to flush/fsync report file at start", exc_info=True)
            except Exception:
                report_file = None

        base = Path(base_path)
        if not base.exists():
            if report_file:
                try: report_file.close()
                except Exception: pass
            return {'files_changed': 0, 'dirs_changed': 0, 'errors': 0, 'fixed_files': [], 'fixed_dirs': [], 'report_path': report_path}

        for root, dirs, files in os.walk(str(base)):
            # Apply directory permissions
            for d in dirs:
                p = os.path.join(root, d)
                try:
                    current_mode = os.stat(p).st_mode & 0o777
                    if current_mode != dir_mode:
                        os.chmod(p, dir_mode)
                        dirs_changed += 1
                        if collect_list and len(fixed_dirs) < _MAX_SAMPLES:
                            fixed_dirs.append(str(p))
                        if report_file:
                            try:
                                report_file.write(f"D\t{p}\n")
                                try:
                                    report_file.flush()
                                    try:
                                        os.fsync(report_file.fileno())
                                    except Exception:
                                        pass
                                except Exception:
                                    logger.debug("Failed to flush/fsync report file after D write", exc_info=True)
                            except Exception:
                                logger.debug("Failed to write directory entry to report file", exc_info=True)
                except Exception:
                    errors += 1

            # Apply file permissions
            for f in files:
                p = os.path.join(root, f)
                try:
                    current_mode = os.stat(p).st_mode & 0o777
                    if current_mode != file_mode:
                        os.chmod(p, file_mode)
                        files_changed += 1
                        if collect_list and len(fixed_files) < _MAX_SAMPLES:
                            fixed_files.append(str(p))
                        if report_file:
                            try:
                                report_file.write(f"F\t{p}\n")
                                try:
                                    report_file.flush()
                                    try:
                                        os.fsync(report_file.fileno())
                                    except Exception:
                                        pass
                                except Exception:
                                    logger.debug("Failed to flush/fsync report file after F write", exc_info=True)
                            except Exception:
                                logger.debug("Failed to write file entry to report file", exc_info=True)
                except Exception:
                    errors += 1

    except Exception:
        errors += 1
    finally:
        if report_file:
            try:
                report_file.write(f"# Completed: files_changed={files_changed} dirs_changed={dirs_changed} errors={errors}\n")
                try:
                    report_file.flush()
                    try:
                        os.fsync(report_file.fileno())
                    except Exception:
                        pass
                except Exception:
                    logger.debug("Failed to flush/fsync report file at completion", exc_info=True)
            except Exception:
                logger.debug("Failed to write completion line to report file", exc_info=True)
            try:
                report_file.close()
            except Exception:
                logger.debug("Failed to close report file", exc_info=True)

    out = {'files_changed': files_changed, 'dirs_changed': dirs_changed, 'errors': errors}
    # Always include fixed_files/fixed_dirs keys for a stable return schema
    out['fixed_files'] = fixed_files
    out['fixed_dirs'] = fixed_dirs
    out['report_path'] = report_path
    return out

    # NOTE: unreachable, kept for clarity



def format_mode(mode):
    """Return a human-readable permission string for a mode.

    Accepts an int (mode) or a string like '0o644'. Returns a string like '0644 (rw-r--r--)'.
    """
    try:
        if isinstance(mode, str):
            if mode.startswith('0o'):
                m = int(mode, 8)
            elif mode.isdigit():
                m = int(mode, 8)
            else:
                m = int(mode)
        else:
            m = int(mode)
    except Exception:
        return str(mode)

    perms = m & 0o777
    oct_str = format(perms, '04o')
    def triplet(bits):
        r = 'r' if perms & bits[0] else '-'
        w = 'w' if perms & bits[1] else '-'
        x = 'x' if perms & bits[2] else '-'
        return r + w + x

    human = triplet((0o400,0o200,0o100)) + triplet((0o040,0o020,0o010)) + triplet((0o004,0o002,0o001))
    return f"{oct_str} ({human})"

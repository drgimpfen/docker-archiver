"""
GFS (Grandfather-Father-Son) retention logic.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from app import utils
from app.utils import setup_logging, get_logger

# Configure logging using centralized setup so LOG_LEVEL is respected
setup_logging()
logger = get_logger(__name__)


ARCHIVE_BASE = utils.get_archives_path()


def run_retention(archive_config, job_id, is_dry_run=False, log_callback=None):
    """
    Run GFS retention cleanup for an archive.
    
    Args:
        archive_config: Archive configuration dict
        job_id: Job ID for logging
        is_dry_run: Whether to simulate only
        log_callback: Function to call for logging
    
    Returns:
        Total bytes reclaimed
    """
    def log(level, msg):
        if log_callback:
            log_callback(level, msg)
        else:
            if level == 'ERROR':
                logger.error("%s", msg)
            elif level == 'WARNING':
                logger.warning("%s", msg)
            else:
                logger.info("%s", msg)
    
    archive_name = archive_config['name']
    keep_days = archive_config.get('retention_keep_days', 7)
    keep_weeks = archive_config.get('retention_keep_weeks', 4)
    keep_months = archive_config.get('retention_keep_months', 6)
    keep_years = archive_config.get('retention_keep_years', 2)
    one_per_day = archive_config.get('retention_one_per_day', False)
    
    log('INFO', f"Starting retention for '{archive_name}':")
    log('INFO', f"  Keep: {keep_days} days, {keep_weeks} weeks, {keep_months} months, {keep_years} years")
    if one_per_day:
        log('INFO', f"  Mode: One archive per day")
    
    archive_dir = Path(ARCHIVE_BASE) / archive_name
    if not archive_dir.exists():
        log('WARNING', f"Archive directory does not exist: {archive_dir}")
        return 0
    
    # Collect archive candidates with more strict rules:
    # - Only inspect first-level entries in archive directory.
    # - Accept names that START with a timestamp (e.g. 20251222_031843_beszel.tar.zst or
    #   20251222_031843_beszel/).
    # - Also support layout /archives/<archive>/<stackname>/<TIMESTAMP_Stack> by inspecting
    #   one level deep inside stack directories (do NOT recurse deeply).
    from collections import defaultdict as _dd

    candidates = []
    import re

    ts_re = re.compile(r"^(\d{8}_\d{6})(?:[_-](.*))?$")

    def strip_ext(name: str):
        for ext in ('.tar.gz', '.tar.zst', '.tar'):
            if name.endswith(ext):
                return name[:-len(ext)], ext
        return name, ''

    # Inspect top-level entries only
    for item in archive_dir.iterdir():
        try:
            if not (item.is_file() or item.is_dir()):
                continue

            name = item.name
            base, ext = strip_ext(name)

            # Case 1: entry itself starts with timestamp -> treat as archive
            m = ts_re.match(base)
            if m:
                ts_str = m.group(1)
                remainder = (m.group(2) or '').lstrip('_-')
                stack_name = remainder or 'unknown'

                try:
                    timestamp = datetime.strptime(ts_str, '%Y%m%d_%H%M%S')
                    local_tz = utils.get_display_timezone()
                    timestamp_local = timestamp.replace(tzinfo=local_tz)
                    timestamp_utc = timestamp_local.astimezone(timezone.utc)
                except Exception:
                    timestamp_utc = timestamp.replace(tzinfo=timezone.utc)

                # Determine size (for directories only sum one level if dir)
                if item.is_file():
                    size = item.stat().st_size
                    is_dir = False
                else:
                    # Sum entire directory contents
                    size = sum(f.stat().st_size for f in item.rglob('*') if f.is_file())
                    is_dir = True

                candidates.append({
                    'path': item,
                    'timestamp': timestamp_utc,
                    'size': size,
                    'is_dir': is_dir,
                    'stack_name': stack_name
                })
                continue

            # Case 2: item is a stack directory -> inspect one level deep for timestamped entries
            if item.is_dir():
                stack_dir = item
                for child in stack_dir.iterdir():
                    try:
                        child_name = child.name
                        child_base, child_ext = strip_ext(child_name)
                        m = ts_re.match(child_base)
                        if not m:
                            continue

                        ts_str = m.group(1)
                        try:
                            timestamp = datetime.strptime(ts_str, '%Y%m%d_%H%M%S')
                            local_tz = utils.get_display_timezone()
                            timestamp_local = timestamp.replace(tzinfo=local_tz)
                            timestamp_utc = timestamp_local.astimezone(timezone.utc)
                        except Exception:
                            timestamp_utc = timestamp.replace(tzinfo=timezone.utc)

                        # For this layout, use the directory name as the stack name
                        stack_name = stack_dir.name or 'unknown'

                        if child.is_file():
                            size = child.stat().st_size
                            is_dir = False
                        else:
                            # Only sum one level to avoid deep recursion
                            size = sum(f.stat().st_size for f in child.rglob('*') if f.is_file())
                            is_dir = True

                        candidates.append({
                            'path': child,
                            'timestamp': timestamp_utc,
                            'size': size,
                            'is_dir': is_dir,
                            'stack_name': stack_name
                        })
                    except Exception as e:
                        log('WARNING', f"Could not parse archive timestamp from {child}: {e}")
                        continue
        except Exception as e:
            log('WARNING', f"Error scanning {item}: {e}")
            continue

    # Group by parsed stack_name
    grouped = _dd(list)
    for c in candidates:
        grouped[c['stack_name']].append(c)

    total_reclaimed = 0
    total_deleted = 0
    total_deleted_dirs = 0
    total_deleted_files = 0

    log('INFO', f"Found {len(grouped)} stack(s) to evaluate for retention")

    for stack_name, archives in grouped.items():
        log('INFO', f"Processing retention for stack: {stack_name}")

        # Sort by timestamp (newest first)
        archives.sort(key=lambda a: a['timestamp'], reverse=True)

        log('INFO', f"Found {len(archives)} archive(s) for {stack_name}")

        # Apply one-per-day filter if enabled
        if one_per_day:
            filtered_archives, duplicates_to_delete = filter_one_per_day(archives, log)
            archives = filtered_archives
        else:
            duplicates_to_delete = []

        # Determine which archives to keep based on GFS rules
        to_keep = apply_gfs_retention(archives, keep_days, keep_weeks, keep_months, keep_years)
        to_delete = [a for a in archives if a not in to_keep] + duplicates_to_delete

        log('INFO', f"Retention will keep {len(to_keep)} archive(s) and delete {len(to_delete)}")

        # Delete old archives
        for archive in to_delete:
            path = archive['path']
            size = archive['size']
            size_mb = size / (1024 * 1024)

            log('INFO', f"Deleting archive: {path.name} ({size_mb:.1f}M)")

            if not is_dry_run:
                try:
                    if archive['is_dir']:
                        import shutil
                        shutil.rmtree(path)
                        total_deleted_dirs += 1
                    else:
                        path.unlink()
                        total_deleted_files += 1
                    total_reclaimed += size
                    total_deleted += 1

                    # Mark as deleted in database
                    _mark_archive_as_deleted(str(path), 'retention')
                except Exception as e:
                    log('ERROR', f"Failed to delete {path.name}: {e}")
                    logger.exception("[Retention] Failed to delete %s: %s", path.name, e)
            else:
                total_reclaimed += size
                if archive['is_dir']:
                    total_deleted_dirs += 1
                else:
                    total_deleted_files += 1
                total_deleted += 1
    
    reclaimed_mb = total_reclaimed / (1024 * 1024)
    reclaimed_gb = total_reclaimed / (1024 * 1024 * 1024)
    
    if reclaimed_gb >= 1:
        size_str = f"{reclaimed_gb:.2f}GB"
    else:
        size_str = f"{reclaimed_mb:.1f}MB"
    
    if total_deleted == 0:
        log('INFO', "Retention cleanup finished. No archives needed deletion.")
    else:
        log('INFO', f"Retention cleanup finished. Deleted {total_deleted} archive(s) ({total_deleted_dirs} directories, {total_deleted_files} files), freeing {size_str}.")
    
    # Return structured results for callers to update DB / notifications
    return {
        'reclaimed': total_reclaimed,
        'deleted': total_deleted,
        'deleted_dirs': total_deleted_dirs,
        'deleted_files': total_deleted_files
    }


def filter_one_per_day(archives, log):
    """Keep only the newest archive per day, mark others for deletion."""
    # Group by date
    by_date = defaultdict(list)
    for archive in archives:
        # Use display timezone date for one-per-day grouping so 'days' align with user-visible dates
        try:
            local_date = archive['timestamp'].astimezone(utils.get_display_timezone()).date()
        except Exception:
            local_date = archive['timestamp'].date()
        by_date[local_date].append(archive)
    
    log('INFO', f"One-per-day filter: Found {len(by_date)} unique date(s)")
    
    # Keep newest per day, mark rest for deletion
    filtered = []
    to_delete = []
    
    for date, day_archives in by_date.items():
        # Sort by timestamp, keep newest
        day_archives.sort(key=lambda a: a['timestamp'], reverse=True)
        filtered.append(day_archives[0])
        
        log('INFO', f"Date {date}: {len(day_archives)} archive(s) - keeping newest: {day_archives[0]['path'].name}")
        
        # Mark older archives from same day for deletion
        if len(day_archives) > 1:
            duplicates = day_archives[1:]
            to_delete.extend(duplicates)
            for dup in duplicates:
                log('INFO', f"  Marking for deletion: {dup['path'].name}")
    
    # Sort again by timestamp
    filtered.sort(key=lambda a: a['timestamp'], reverse=True)
    return filtered, to_delete


def apply_gfs_retention(archives, keep_days, keep_weeks, keep_months, keep_years):
    """
    Apply GFS retention rules.
    
    Returns list of archives to keep.
    """
    now = utils.now()
    to_keep = []
    
    # Sort archives by timestamp (newest first)
    archives = sorted(archives, key=lambda a: a['timestamp'], reverse=True)
    
    # Keep daily archives (last N days)
    daily_cutoff = now - timedelta(days=keep_days)
    for archive in archives:
        if archive['timestamp'] >= daily_cutoff:
            if archive not in to_keep:
                to_keep.append(archive)
    
    # Keep weekly archives (last N weeks, one per week)
    weekly_kept = set()
    for i in range(keep_weeks):
        week_start = now - timedelta(weeks=i+1)
        week_end = now - timedelta(weeks=i)
        
        for archive in archives:
            ts = archive['timestamp']
            if week_start <= ts < week_end:
                week_key = ts.isocalendar()[1]  # ISO week number
                if week_key not in weekly_kept:
                    if archive not in to_keep:
                        to_keep.append(archive)
                    weekly_kept.add(week_key)
                    break
    
    # Keep monthly archives (last N months, one per month)
    monthly_kept = set()
    for i in range(keep_months):
        # Calculate month boundaries
        if now.month - i > 0:
            month = now.month - i
            year = now.year
        else:
            month = 12 + (now.month - i)
            year = now.year - 1
        
        for archive in archives:
            ts = archive['timestamp']
            if ts.year == year and ts.month == month:
                month_key = (year, month)
                if month_key not in monthly_kept:
                    if archive not in to_keep:
                        to_keep.append(archive)
                    monthly_kept.add(month_key)
                    break
    
    # Keep yearly archives (last N years, one per year)
    yearly_kept = set()
    for i in range(keep_years):
        year = now.year - i
        
        for archive in archives:
            ts = archive['timestamp']
            if ts.year == year:
                if year not in yearly_kept:
                    if archive not in to_keep:
                        to_keep.append(archive)
                    yearly_kept.add(year)
                    break
    
    return to_keep


def _mark_archive_as_deleted(archive_path, deleted_by='retention'):
    """Mark archive as deleted in database."""
    try:
        from app.db import get_db
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE job_stack_metrics 
                SET deleted_at = NOW(), deleted_by = %s
                WHERE archive_path = %s AND deleted_at IS NULL;
            """, (deleted_by, archive_path))
            conn.commit()
    except Exception as e:
        logger.exception("[Retention] Failed to mark archive as deleted in DB: %s", e)

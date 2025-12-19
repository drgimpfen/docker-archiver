"""
GFS (Grandfather-Father-Son) retention logic.
"""
import os
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from app import utils


ARCHIVE_BASE = '/archives'


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
            print(f"[{level}] {msg}")
    
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
    
    # Collect all archives grouped by stack
    stacks = [d for d in archive_dir.iterdir() if d.is_dir()]
    
    total_reclaimed = 0
    total_deleted = 0
    
    for stack_dir in stacks:
        stack_name = stack_dir.name
        log('INFO', f"Processing retention for stack: {stack_name}")
        
        # Get all archive files/folders in this stack directory
        archives = []
        for item in stack_dir.iterdir():
            # Parse timestamp from filename: stackname_YYYYMMDD_HHMMSS.ext
            try:
                name = item.name
                # Remove extension(s)
                if name.endswith('.tar.gz'):
                    base = name[:-7]
                elif name.endswith('.tar.zst'):
                    base = name[:-8]
                elif name.endswith('.tar'):
                    base = name[:-4]
                else:
                    base = name
                
                # Extract timestamp: last part after underscore should be YYYYMMDD_HHMMSS
                parts = base.split('_')
                if len(parts) >= 3:
                    date_str = parts[-2]  # YYYYMMDD
                    time_str = parts[-1]  # HHMMSS
                    timestamp = datetime.strptime(f"{date_str}_{time_str}", '%Y%m%d_%H%M%S')
                    
                    # Get size
                    if item.is_file():
                        size = item.stat().st_size
                    else:  # directory
                        size = sum(f.stat().st_size for f in item.rglob('*') if f.is_file())
                    
                    archives.append({
                        'path': item,
                        'timestamp': timestamp,
                        'size': size,
                        'is_dir': item.is_dir()
                    })
            except Exception as e:
                log('WARNING', f"Could not parse archive timestamp from {item.name}: {e}")
                continue
        
        if not archives:
            log('INFO', f"No archives found for {stack_name}")
            continue
        
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
                    else:
                        path.unlink()
                    total_reclaimed += size
                    total_deleted += 1
                    
                    # Mark as deleted in database
                    _mark_archive_as_deleted(str(path), 'retention')
                except Exception as e:
                    log('ERROR', f"Failed to delete {path.name}: {e}")
            else:
                total_reclaimed += size
                total_deleted += 1
    
    reclaimed_mb = total_reclaimed / (1024 * 1024)
    reclaimed_gb = total_reclaimed / (1024 * 1024 * 1024)
    
    if reclaimed_gb >= 1:
        size_str = f"{reclaimed_gb:.2f}GB"
    else:
        size_str = f"{reclaimed_mb:.1f}MB"
    
    log('INFO', f"Retention cleanup finished. Total files deleted: {total_deleted}. Freed space: {size_str}")
    
    return total_reclaimed


def filter_one_per_day(archives, log):
    """Keep only the newest archive per day, mark others for deletion."""
    # Group by date
    by_date = defaultdict(list)
    for archive in archives:
        date_key = archive['timestamp'].date()
        by_date[date_key].append(archive)
    
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
        print(f"[Retention] Failed to mark archive as deleted in DB: {e}")

#!/usr/bin/env python3
"""
Migrate timestamped archives that live at the top level
into per-stack subfolders: \n
Before: /archives/<archive>/20251223_001245_borg-ui.tar.zst
After:  /archives/<archive>/borg-ui/20251223_001245_borg-ui.tar.zst

Usage:
  python tools/migrate_archives.py [--dry-run] [--archive <name>]
"""
import argparse
import logging
import re
import shutil
from pathlib import Path
from app.utils import get_archives_path, setup_logging, get_logger

setup_logging()
logger = get_logger(__name__)

TS_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})[_-](?P<stack>[^/]+)(?P<ext>\..+)?$")


def migrate(archives_base: Path, archive_name: str = None, dry_run: bool = True):
    archives_base = Path(archives_base)
    if archive_name:
        archive_paths = [archives_base / archive_name]
    else:
        archive_paths = [p for p in archives_base.iterdir() if p.is_dir()]

    moved = 0
    skipped = 0
    errors = 0

    for ad in archive_paths:
        if not ad.exists():
            logger.warning("Archive dir does not exist: %s", ad)
            continue
        logger.info("Scanning archive: %s", ad)
        for item in ad.iterdir():
            name = item.name
            m = TS_RE.match(name)
            if not m:
                # Not a timestamp_stack file/dir
                continue
            stack = m.group('stack')
            # target dir is archive/<stack>
            dest_dir = ad / stack
            dest = dest_dir / name
            if dest.exists():
                logger.info("Destination exists, skipping: %s", dest)
                skipped += 1
                continue
            logger.info("Will move: %s -> %s", item, dest)
            if dry_run:
                moved += 1
                continue
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(dest))
                logger.info("Moved: %s -> %s", item, dest)
                moved += 1
            except Exception as e:
                logger.error("Failed to move %s: %s", item, e)
                errors += 1

    return moved, skipped, errors


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true', default=False, help='Show what would be migrated')
    p.add_argument('--archive', type=str, help='Limit migration to a single archive name')
    args = p.parse_args()

    archives_base = Path(get_archives_path())
    m, s, e = migrate(archives_base, archive_name=args.archive, dry_run=args.dry_run)
    logger.info('Summary: planned moves=%d skipped=%d errors=%d (dry_run=%s)', m, s, e, args.dry_run)
    if args.dry_run:
        logger.info('Dry-run mode: no files were moved')

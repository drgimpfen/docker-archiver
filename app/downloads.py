"""
Download token system for archive downloads.
"""
import secrets
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import os, shutil
from app.db import get_db
from app import utils
from app.utils import setup_logging, get_logger

# Configure logging using centralized setup so LOG_LEVEL is respected
setup_logging()
logger = get_logger(__name__)

# Directory where generated download archives live (default: container tmp)
# Fixed downloads directory - always use /tmp/downloads (not configurable)
DOWNLOADS_PATH = utils.get_downloads_path()
try:
    DOWNLOADS_PATH.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


def generate_download_token(job_id, stack_name, archive_path, is_folder=False, expires_hours=24):
    """
    Generate a secure download token for an archive file.
    
    Returns: token string
    """
    token = secrets.token_urlsafe(32)
    expires_at = utils.now() + timedelta(hours=expires_hours)
    
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO download_tokens (token, job_id, stack_name, archive_path, is_folder, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id;
        """, (token, job_id, stack_name, archive_path, is_folder, expires_at))
        conn.commit()
    
    return token


def get_download_by_token(token):
    """
    Legacy helper kept for compatibility: returns download info and increments the
    downloads counter if token is valid.
    """
    with get_db() as conn:
        cur = conn.cursor()
        # Get max downloads setting
        cur.execute("SELECT value FROM settings WHERE key = 'max_token_downloads';")
        setting = cur.fetchone()
        max_downloads = int(setting['value']) if setting else 3

        cur.execute("""
            SELECT * FROM download_tokens 
            WHERE token = %s 
            AND expires_at > CURRENT_TIMESTAMP
            AND downloads < %s;
        """, (token, max_downloads))
        result = cur.fetchone()

        if result:
            # Increment download counter
            cur.execute("""
                UPDATE download_tokens 
                SET downloads = downloads + 1 
                WHERE token = %s;
            """, (token,))
            conn.commit()

        return result


def get_download_token_row(token):
    """Return the raw token row (no checks) or None if not found."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM download_tokens WHERE token = %s;", (token,))
        return cur.fetchone()


def increment_download_count(token):
    """Increment downloads counter for a token."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE download_tokens SET downloads = downloads + 1 WHERE token = %s;", (token,))
        conn.commit()


def prepare_archive_for_download(file_path, output_format='tar.gz'):
    """
    Prepare archive for download.
    If file_path is a directory, create compressed archive.
    
    Returns: (actual_file_path, should_cleanup)
    """
    path = Path(file_path)
    
    # If it's already a file, return as-is
    if path.is_file():
        return str(path), False
    
    # If it's a directory, create temporary archive in DOWNLOADS_PATH
    if path.is_dir():
        timestamp = utils.local_now().strftime('%Y%m%d_%H%M%S')
        safe_name = utils.filename_safe(path.name)
        # Ensure downloads dir exists
        try:
            DOWNLOADS_PATH.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        if output_format == 'tar.gz':
            temp_file = DOWNLOADS_PATH / f"{timestamp}_download_{safe_name}.tar.gz"
            cmd_parts = ['tar', '-czf', str(temp_file), '-C', str(path.parent), path.name]
        elif output_format == 'tar.zst':
            temp_file = DOWNLOADS_PATH / f"{timestamp}_download_{safe_name}.tar.zst"
            cmd_parts = ['tar', '--use-compress-program=zstd', '-cf', str(temp_file), '-C', str(path.parent), path.name]
        else:  # tar
            temp_file = DOWNLOADS_PATH / f"{timestamp}_download_{safe_name}.tar"
            cmd_parts = ['tar', '-cf', str(temp_file), '-C', str(path.parent), path.name]

        logger.info("[Downloads] Creating archive for download: %s", temp_file)

        try:
            result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                return str(temp_file), True
            else:
                raise Exception(f"Failed to create archive: {result.stderr}")
        except Exception as e:
            logger.exception("[Downloads] Error creating archive: %s", e)
            return None, False
    
    return None, False


def cleanup_expired_tokens():
    """Delete expired download tokens and their temporary files.

    Also deletes temporary files created for downloads in DOWNLOADS_PATH and tokens
    that have reached their maximum download count.
    """
    logger.info("[Downloads] Running token cleanup...")

    with get_db() as conn:
        cur = conn.cursor()

        # Get max downloads setting
        cur.execute("SELECT value FROM settings WHERE key = 'max_token_downloads';")
        setting = cur.fetchone()
        max_downloads = int(setting['value']) if setting else 3

        # Find tokens that are expired or exhausted (downloads >= max)
        cur.execute("""
            SELECT token, archive_path FROM download_tokens
            WHERE expires_at <= CURRENT_TIMESTAMP OR downloads >= %s;
        """, (max_downloads,))
        bad_tokens = cur.fetchall()

        deleted_count = 0
        for row in bad_tokens:
            file_path = row['archive_path'] and Path(row['archive_path'])
            if file_path and file_path.exists():
                # Only delete files that look like generated download files
                if '_download_' in file_path.name or str(DOWNLOADS_PATH) in str(file_path):
                    try:
                        file_path.unlink()
                        deleted_count += 1
                    except Exception as e:
                        logger.exception("[Downloads] Failed to delete temp file %s: %s", file_path, e)

        # Delete expired or exhausted tokens from database
        cur.execute("DELETE FROM download_tokens WHERE expires_at <= CURRENT_TIMESTAMP OR downloads >= %s;", (max_downloads,))
        tokens_deleted = cur.rowcount
        conn.commit()

        logger.info("[Downloads] Cleanup complete: %s token(s) deleted, %s temp file(s) deleted", tokens_deleted, deleted_count)


def startup_rescan_downloads():
    """Rescan active download tokens and attempt to regenerate missing files where possible."""
    logger.info("[Downloads] Starting startup rescan of download tokens...")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM download_tokens WHERE expires_at > CURRENT_TIMESTAMP;")
        tokens = cur.fetchall()

        for t in tokens:
            token = t['token']
            archive_path = t.get('archive_path')
            is_folder = t.get('is_folder')

            if not archive_path:
                # Nothing to do
                continue

            path = Path(archive_path)
            if path.exists():
                # File exists, nothing to do
                continue

            logger.info("[Downloads] Missing file for token %s: %s", token, archive_path)

            # If token originally referred to a folder, and the folder still exists, create an archive
            if is_folder and path.is_dir():
                logger.info("[Downloads] Regenerating archive for folder %s for token %s...", archive_path, token)
                new_path, should_cleanup = prepare_archive_for_download(archive_path)
                if new_path:
                    cur.execute("UPDATE download_tokens SET archive_path = %s, is_folder = false WHERE token = %s;", (new_path, token))
                    conn.commit()
                    logger.info("[Downloads] Regenerated and updated token %s -> %s", token, new_path)
                    continue

            # Try to find original archive path from job_stack_metrics using job_id and stack_name
            job_id = t.get('job_id')
            stack_name = t.get('stack_name')
            if job_id and stack_name:
                cur.execute("""
                    SELECT archive_path FROM job_stack_metrics
                    WHERE job_id = %s AND stack_name = %s AND archive_path IS NOT NULL
                    ORDER BY start_time DESC LIMIT 1;
                """, (job_id, stack_name))
                row = cur.fetchone()
                if row and row.get('archive_path'):
                    candidate = Path(row['archive_path'])
                    if candidate.exists():
                        # If it's a file, copy it into DOWNLOADS_PATH
                        if candidate.is_file():
                            try:
                                new_name = f"{utils.local_now().strftime('%Y%m%d_%H%M%S')}_download_{utils.filename_safe(candidate.name)}{candidate.suffix}"
                                dest = DOWNLOADS_PATH / new_name
                                shutil.copy2(str(candidate), str(dest))
                                cur.execute("UPDATE download_tokens SET archive_path = %s, is_preparing = false WHERE token = %s;", (str(dest), token))
                                conn.commit()
                                logger.info("[Downloads] Restored download for token %s from job metric: %s", token, dest)
                                continue
                            except Exception as e:
                                logger.exception("[Downloads] Failed to restore file for token %s from %s: %s", token, candidate, e)

                        # If it's a directory, create an archive for download
                        if candidate.is_dir():
                            try:
                                logger.info("[Downloads] Found directory for token %s, creating archive...", token)
                                # Prefer compressed zstd archives for downloads when creating from folders
                                new_path, should_cleanup = prepare_archive_for_download(str(candidate), output_format='tar.zst')
                                if new_path:
                                    cur.execute("UPDATE download_tokens SET archive_path = %s, is_folder = false, is_preparing = false WHERE token = %s;", (str(new_path), token))
                                    conn.commit()
                                    logger.info("[Downloads] Created archive %s for token %s from directory %s", new_path, token, candidate)
                                    continue
                            except Exception as e:
                                logger.exception("[Downloads] Failed to create archive for token %s from directory %s: %s", token, candidate, e)

            # Could not regenerate; optionally mark token as invalid by expiring it
            try:
                cur.execute("UPDATE download_tokens SET expires_at = NOW() WHERE token = %s;", (token,))
                conn.commit()
                logger.info("[Downloads] Marked token %s as expired due to missing file", token)
            except Exception as e:
                logger.exception("[Downloads] Failed to mark token %s expired: %s", token, e)

    logger.info("[Downloads] Startup rescan complete.")


def regenerate_token(token):
    """Attempt to regenerate a download file for a specific token into DOWNLOADS_PATH.

    Sets `is_preparing` to True at the start and clears it at the end.
    """
    logger.info("[Downloads] Starting regeneration for token %s", token)
    with get_db() as conn:
        cur = conn.cursor()
        # Mark as preparing if not already
        try:
            cur.execute("SELECT is_preparing, archive_path, is_folder, job_id, stack_name FROM download_tokens WHERE token = %s;", (token,))
            row = cur.fetchone()
            if not row:
                logger.warning("[Downloads] regenerate_token: token %s not found", token)
                return False
            if row.get('is_preparing'):
                logger.info("[Downloads] regenerate_token: token %s already preparing", token)
                return False
            cur.execute("UPDATE download_tokens SET is_preparing = true WHERE token = %s;", (token,))
            conn.commit()
        except Exception as e:
                logger.exception("[Downloads] Failed to mark token %s as preparing: %s", token, e)
                return False
    success = False
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT archive_path, is_folder, job_id, stack_name FROM download_tokens WHERE token = %s;", (token,))
            trow = cur.fetchone()
            if not trow:
                logger.warning("[Downloads] regenerate_token: token %s disappeared", token)
                return False
            archive_path = trow.get('archive_path')
            is_folder = trow.get('is_folder')

            if not archive_path:
                logger.warning("[Downloads] regenerate_token: token %s has no archive_path", token)
                return False

            candidate = Path(archive_path)
            if candidate.exists():
                # If candidate points outside DOWNLOADS_PATH, copy/prepare into DOWNLOADS_PATH
                if candidate.is_file():
                    try:
                        new_name = f"{utils.local_now().strftime('%Y%m%d_%H%M%S')}_download_{utils.filename_safe(candidate.name)}{candidate.suffix}"
                        dest = DOWNLOADS_PATH / new_name
                        shutil.copy2(str(candidate), str(dest))
                        cur.execute("UPDATE download_tokens SET archive_path = %s, is_preparing = false WHERE token = %s;", (str(dest), token))
                        conn.commit()
                        logger.info("[Downloads] regenerate_token: Restored token %s from %s -> %s", token, candidate, dest)
                        success = True
                    except Exception as e:
                        logger.exception("[Downloads] regenerate_token: Failed to copy candidate %s for token %s: %s", candidate, token, e)
                elif candidate.is_dir():
                    try:
                        new_path, did_cleanup = prepare_archive_for_download(str(candidate), output_format='tar.zst')
                        if new_path and Path(new_path).exists():
                            cur.execute("UPDATE download_tokens SET archive_path = %s, is_folder = false, is_preparing = false WHERE token = %s;", (str(new_path), token))
                            conn.commit()
                            logger.info("[Downloads] regenerate_token: Created archive %s for token %s from directory %s", new_path, token, candidate)
                            success = True
                    except Exception as e:
                        logger.exception("[Downloads] regenerate_token: Failed to create archive from %s for token %s: %s", candidate, token, e)
            else:
                # Try to find from job_stack_metrics
                job_id = trow.get('job_id')
                stack_name = trow.get('stack_name')
                if job_id and stack_name:
                    cur.execute("""
                        SELECT archive_path FROM job_stack_metrics
                        WHERE job_id = %s AND stack_name = %s AND archive_path IS NOT NULL
                        ORDER BY start_time DESC LIMIT 1;
                    """, (job_id, stack_name))
                    row2 = cur.fetchone()
                    if row2 and row2.get('archive_path'):
                        cand = Path(row2['archive_path'])
                        if cand.exists():
                            if cand.is_file():
                                try:
                                    new_name = f"{utils.local_now().strftime('%Y%m%d_%H%M%S')}_download_{utils.filename_safe(cand.name)}{cand.suffix}"
                                    dest = DOWNLOADS_PATH / new_name
                                    shutil.copy2(str(cand), str(dest))
                                    cur.execute("UPDATE download_tokens SET archive_path = %s, is_preparing = false WHERE token = %s;", (str(dest), token))
                                    conn.commit()
                                    logger.info("[Downloads] regenerate_token: Restored token %s from job metric %s -> %s", token, cand, dest)
                                    success = True
                                except Exception as e:
                                    logger.exception("[Downloads] regenerate_token: Failed to copy candidate file %s for token %s: %s", cand, token, e)
                            elif cand.is_dir():
                                try:
                                    new_path, did_cleanup = prepare_archive_for_download(str(cand), output_format='tar.zst')
                                    if new_path and Path(new_path).exists():
                                        cur.execute("UPDATE download_tokens SET archive_path = %s, is_folder = false, is_preparing = false WHERE token = %s;", (str(new_path), token))
                                        conn.commit()
                                        logger.info("[Downloads] regenerate_token: Created archive %s for token %s from job metric %s", new_path, token, cand)
                                        success = True
                                except Exception as e:
                                    logger.exception("[Downloads] regenerate_token: Failed to create archive from %s for token %s: %s", cand, token, e)

    except Exception as e:
        logger.exception("[Downloads] regenerate_token: Unexpected error for token %s: %s", token, e)
    finally:
        try:
            # Ensure we clear is_preparing if we didn't already set the new archive_path
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT archive_path, is_preparing FROM download_tokens WHERE token = %s;", (token,))
                r = cur.fetchone()
                if r and (not r.get('archive_path')) and r.get('is_preparing'):
                    cur.execute("UPDATE download_tokens SET is_preparing = false WHERE token = %s;", (token,))
                    conn.commit()
                    logger.info("[Downloads] regenerate_token: Cleared is_preparing for token %s", token)
        except Exception:
            pass

    logger.info("[Downloads] regenerate_token complete for token %s (success=%s)", token, success)
    return success

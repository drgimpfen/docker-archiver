"""
Download token system for archive downloads.
"""
import secrets
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from app.db import get_db
from app import utils


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
    Get download info by token.
    
    Args:
        token: Download token
    
    Returns: dict with download info or None if invalid/expired/exhausted
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
    
    # If it's a directory, create temporary archive
    if path.is_dir():
        # Create temp file in same directory
        timestamp = utils.local_now().strftime('%Y%m%d_%H%M%S')
        
        if output_format == 'tar.gz':
            temp_file = path.parent / f"{path.name}_download_{timestamp}.tar.gz"
            cmd_parts = ['tar', '-czf', str(temp_file), '-C', str(path.parent), path.name]
        elif output_format == 'tar.zst':
            temp_file = path.parent / f"{path.name}_download_{timestamp}.tar.zst"
            cmd_parts = ['tar', '--use-compress-program=zstd', '-cf', str(temp_file), '-C', str(path.parent), path.name]
        else:  # tar
            temp_file = path.parent / f"{path.name}_download_{timestamp}.tar"
            cmd_parts = ['tar', '-cf', str(temp_file), '-C', str(path.parent), path.name]
        
        print(f"[Downloads] Creating archive for download: {temp_file}")
        
        try:
            result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                return str(temp_file), True
            else:
                raise Exception(f"Failed to create archive: {result.stderr}")
        except Exception as e:
            print(f"[Downloads] Error creating archive: {e}")
            return None, False
    
    return None, False


def cleanup_expired_tokens():
    """Delete expired download tokens and their temporary files."""
    print("[Downloads] Running token cleanup...")
    
    with get_db() as conn:
        cur = conn.cursor()
        
        # Get expired tokens with temp files (archive_path is the current column name)
        cur.execute("""
            SELECT archive_path FROM download_tokens 
            WHERE expires_at <= CURRENT_TIMESTAMP
            AND archive_path LIKE '%_download_%';
        """)
        temp_files = cur.fetchall()
        
        # Delete temp files
        deleted_count = 0
        for row in temp_files:
            file_path = Path(row['archive_path'])
            if file_path.exists() and '_download_' in file_path.name:
                try:
                    file_path.unlink()
                    deleted_count += 1
                except Exception as e:
                    print(f"[Downloads] Failed to delete temp file {file_path}: {e}")
        
        # Delete expired tokens from database
        cur.execute("DELETE FROM download_tokens WHERE expires_at <= CURRENT_TIMESTAMP;")
        tokens_deleted = cur.rowcount
        conn.commit()
        
        print(f"[Downloads] Cleanup complete: {tokens_deleted} token(s), {deleted_count} temp file(s) deleted")

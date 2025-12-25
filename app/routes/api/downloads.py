"""
Download tokens API for secure stack archive downloads.
"""
import os
import uuid
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from flask import request, jsonify, send_file
from app.routes.api import bp, api_auth_required
from app.db import get_db
from app.utils import get_downloads_path, get_logger, setup_logging
from app.notifications.adapters.smtp import SMTPAdapter
from app.notifications.core import get_setting

# Configure logging
setup_logging()
logger = get_logger(__name__)


def generate_token():
    """Generate a secure UUID token."""
    return str(uuid.uuid4())


def cleanup_expired_tokens():
    """Remove expired tokens and their associated files."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            # Get expired tokens
            cur.execute("""
                SELECT token, file_path FROM download_tokens
                WHERE expires_at < NOW();
            """)
            expired = cur.fetchall()
            
            # Delete files
            for token_row in expired:
                try:
                    file_path = Path(token_row['file_path'])
                    if file_path.exists():
                        file_path.unlink()
                        logger.info(f"Cleaned up expired download file: {file_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete expired file {token_row['file_path']}: {e}")
            
            # Delete tokens
            cur.execute("DELETE FROM download_tokens WHERE expires_at < NOW();")
            deleted_count = cur.rowcount
            conn.commit()
            
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} expired download tokens")
                
    except Exception as e:
        logger.exception(f"Error during token cleanup: {e}")


def send_download_email(stack_name, download_url):
    """Send download link via email."""
    try:
        adapter = SMTPAdapter()
        base_url = get_setting('base_url', 'http://localhost:8080')
        
        title = f"Stack Archive Download Ready: {stack_name}"
        body = f"""
        <p>Your requested stack archive for <strong>{stack_name}</strong> is ready for download.</p>
        
        <p><a href="{download_url}" class="btn btn-primary">Download Archive</a></p>
        
        <p><small>This link will expire in 24 hours.</small></p>
        
        <p>Best regards,<br>Docker Archiver</p>
        """
        
        result = adapter.send(title, body)
        if result.success:
            logger.info(f"Download email sent for stack {stack_name}")
        else:
            logger.error(f"Failed to send download email for stack {stack_name}: {result.detail}")
            
    except Exception as e:
        logger.exception(f"Error sending download email for stack {stack_name}: {e}")


def pack_stack_directory(stack_name, source_path, output_path):
    """Pack a stack directory into a tar.gz archive."""
    try:
        logger.info(f"Starting to pack stack {stack_name} from {source_path} to {output_path}")
        
        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Use tar to create compressed archive
        cmd = ['tar', '-czf', str(output_path), '-C', str(source_path.parent), source_path.name]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)  # 1 hour timeout
        
        if result.returncode != 0:
            logger.error(f"Failed to pack stack {stack_name}: {result.stderr}")
            return False
        
        logger.info(f"Successfully packed stack {stack_name} to {output_path}")
        return True
        
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout packing stack {stack_name}")
        return False
    except Exception as e:
        logger.exception(f"Error packing stack {stack_name}: {e}")
        return False


def process_directory_pack(stack_name, source_path, token):
    """Background process to pack directory and send email."""
    try:
        # Create output path in temp downloads directory
        downloads_path = get_downloads_path()
        output_filename = f"{stack_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz"
        output_path = downloads_path / output_filename
        
        # Pack the directory
        success = pack_stack_directory(stack_name, Path(source_path), output_path)
        
        if success:
            # Update token with final file path and mark as ready
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE download_tokens 
                    SET file_path = %s, is_packing = FALSE 
                    WHERE token = %s;
                """, (str(output_path), token))
                conn.commit()
            
            # Send email with download link
            base_url = get_setting('base_url', 'http://localhost:8080')
            download_url = f"{base_url}/download/{token}"
            send_download_email(stack_name, download_url)
            
        else:
            # Packing failed, remove token
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM download_tokens WHERE token = %s;", (token,))
                conn.commit()
            logger.error(f"Packing failed for stack {stack_name}, token removed")
            
    except Exception as e:
        logger.exception(f"Error in background packing process for stack {stack_name}: {e}")
        # Clean up token on error
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM download_tokens WHERE token = %s;", (token,))
                conn.commit()
        except Exception:
            pass


@bp.route('/downloads/request', methods=['POST'])
@api_auth_required
def request_download():
    """Request a download for a specific stack archive."""
    data = request.get_json()
    if not data or 'stack_name' not in data or 'archive_path' not in data:
        return jsonify({'error': 'Missing stack_name or archive_path'}), 400
    
    stack_name = data['stack_name']
    archive_path = data['archive_path']
    
    path = Path(archive_path)
    if not path.exists():
        return jsonify({'error': 'Archive path does not exist'}), 404
    
    # Generate token
    token = generate_token()
    expires_at = datetime.now() + timedelta(hours=24)
    
    try:
        with get_db() as conn:
            cur = conn.cursor()
            
            if path.is_file():
                # File exists, create token immediately and send email
                cur.execute("""
                    INSERT INTO download_tokens (token, stack_name, file_path, expires_at, is_packing)
                    VALUES (%s, %s, %s, %s, FALSE);
                """, (token, stack_name, archive_path, expires_at))
                conn.commit()
                
                # Send email immediately
                base_url = get_setting('base_url', 'http://localhost:8080')
                download_url = f"{base_url}/download/{token}"
                send_download_email(stack_name, download_url)
                
                return jsonify({
                    'success': True,
                    'message': 'Download link sent via email',
                    'is_folder': False,
                    'download_url': download_url
                })
                
            elif path.is_dir():
                # Directory needs packing
                cur.execute("""
                    INSERT INTO download_tokens (token, stack_name, file_path, expires_at, is_packing)
                    VALUES (%s, %s, %s, %s, TRUE);
                """, (token, stack_name, archive_path, expires_at))
                conn.commit()
                
                # Start background packing process
                thread = threading.Thread(
                    target=process_directory_pack,
                    args=(stack_name, archive_path, token)
                )
                thread.daemon = True
                thread.start()
                
                return jsonify({
                    'success': True,
                    'message': 'Archive preparation started. You will receive an email when ready.',
                    'is_folder': True
                })
            
            else:
                return jsonify({'error': 'Path is neither file nor directory'}), 400
                
    except Exception as e:
        logger.exception(f"Error creating download token for {stack_name}: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@bp.route('/downloads/tokens')
@api_auth_required
def list_active_tokens():
    """List all active (non-expired) download tokens."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT token, stack_name, created_at, expires_at, is_packing
                FROM download_tokens
                WHERE expires_at > NOW()
                ORDER BY created_at DESC;
            """)
            tokens = cur.fetchall()
        
        return jsonify({
            'tokens': [{
                'token': t['token'],
                'stack_name': t['stack_name'],
                'created_at': t['created_at'].isoformat() if t['created_at'] else None,
                'expires_at': t['expires_at'].isoformat() if t['expires_at'] else None,
                'is_packing': t['is_packing']
            } for t in tokens]
        })
        
    except Exception as e:
        logger.exception("Error listing download tokens")
        return jsonify({'error': 'Internal server error'}), 500


@bp.route('/downloads/cleanup', methods=['POST'])
@api_auth_required
def trigger_cleanup():
    """Manually trigger cleanup of expired tokens."""
    try:
        cleanup_expired_tokens()
        return jsonify({'success': True, 'message': 'Cleanup completed'})
    except Exception as e:
        logger.exception("Error during manual cleanup")
        return jsonify({'error': 'Internal server error'}), 500

"""Download-related API endpoints (token generation, folder preparation)."""
import secrets
import os
import subprocess
import threading
import json
from pathlib import Path
from datetime import timedelta
from flask import request, jsonify
from app.routes.api import bp, api_auth_required
from app.db import get_db
from app.auth import get_current_user
from app import utils


@bp.route('/jobs/<int:job_id>/download', methods=['POST'])
@api_auth_required
def request_download(job_id):
    """Request download for an archive (generates token and prepares file)."""
    try:
        data = request.get_json()
        stack_name = data.get('stack_name')
        archive_path = data.get('archive_path')

        if not archive_path or not os.path.exists(archive_path):
            return jsonify({'error': 'Archive not found'}), 404

        # Check if it's a folder - if yes, we need to create an archive
        is_folder = os.path.isdir(archive_path)

        # Generate download token
        token = secrets.token_urlsafe(32)
        expires_at = utils.now() + timedelta(hours=24)

        # Store token in database
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO download_tokens (token, job_id, stack_name, archive_path, is_folder, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s);
            """, (token, job_id, stack_name, archive_path, is_folder, expires_at))
            conn.commit()

        # If it's a folder, start background compression
        if is_folder:
            threading.Thread(
                target=_prepare_folder_download,
                args=(token, archive_path, stack_name, get_current_user()['email'])
            ).start()

            return jsonify({
                'success': True,
                'message': 'Archive is being prepared. You will receive a notification when ready.',
                'is_folder': True
            })
        else:
            # File is ready, return download link
            base_url = _get_base_url()
            download_url = f"{base_url}/download/{token}"
            return jsonify({
                'success': True,
                'download_url': download_url,
                'token': token,
                'expires_in': '24 hours',
                'is_folder': False
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _prepare_folder_download(token, folder_path, stack_name, user_email):
    try:
        from app import downloads as _downloads
        try:
            _downloads.DOWNLOADS_PATH.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        timestamp = utils.local_now().strftime('%Y%m%d_%H%M%S')
        safe_name = utils.filename_safe(stack_name)
        archive_name = f"{timestamp}_download_{safe_name}.tar.zst"
        archive_path = _downloads.DOWNLOADS_PATH / archive_name
        subprocess.run(['tar', '-I', 'zstd', '-cf', str(archive_path), '-C', str(Path(folder_path).parent), Path(folder_path).name], check=True, timeout=3600)
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE download_tokens 
                SET archive_path = %s, is_folder = false 
                WHERE token = %s;
            """, (str(archive_path), token))
            conn.commit()
        if user_email:
            try:
                # Send direct email via SMTP to the provided user_email
                from app.notifications.adapters import SMTPAdapter
                from app.utils import get_logger
                logger = get_logger(__name__)
                from app.notifications.core import get_setting
                smtp_adapter = SMTPAdapter() if get_setting('smtp_server') else None
                if not smtp_adapter:
                    logger.warning('SMTP not configured; cannot send download notification to %s', user_email)
                else:
                    base_url = _get_base_url()
                    download_url = f"{base_url}/download/{token}"
                    body = f"""<h2>Your archive is ready for download</h2>
<p><strong>Stack:</strong> {stack_name}</p>
<p><a href=\"{download_url}\">Download Archive</a></p>
<p><small>This link will expire in 24 hours</small></p>"""
                    res = smtp_adapter.send("📦 Archive Download Ready", body, body_format=None, attach=None, recipients=[user_email], context=f'download_{token}')
                    if not res.success:
                        logger.error('Failed to send download email to %s: %s', user_email, res.detail)
            except Exception as e:
                from app.utils import get_logger
                get_logger(__name__).exception("Failed to send download notification: %s", e)
    except Exception as e:
        print(f"[ERROR] Failed to prepare download: {e}")


def _get_base_url():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = 'base_url';")
        result = cur.fetchone()
        return result['value'] if result else 'http://localhost:8080'

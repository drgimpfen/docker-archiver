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
import shutil


def _find_existing_archive(job_id, stack_name):
    """Return a path to an existing generated archive for given job/stack, or None."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT archive_path FROM download_tokens
                WHERE job_id = %s AND stack_name = %s AND archive_path IS NOT NULL AND expires_at > NOW()
                ORDER BY created_at DESC LIMIT 1;
            """, (job_id, stack_name))
            row = cur.fetchone()
            if row and row.get('archive_path') and Path(row['archive_path']).is_file():
                return row['archive_path']
    except Exception:
        pass

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT archive_path FROM job_stack_metrics
                WHERE job_id = %s AND stack_name = %s AND archive_path IS NOT NULL
                ORDER BY start_time DESC LIMIT 1;
            """, (job_id, stack_name))
            row = cur.fetchone()
            if row and row.get('archive_path') and Path(row['archive_path']).is_file():
                return row['archive_path']
    except Exception:
        pass

    return None


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

        expires_at = utils.now() + timedelta(hours=24)

        # If it's a folder, try to reuse an existing generated archive (avoid regenerating)
        if is_folder:
            existing_archive = _find_existing_archive(job_id, stack_name)
            if existing_archive:
                try:
                    from app import downloads as _downloads
                    existing_path = Path(existing_archive)
                    if existing_path.exists() and existing_path.is_file():
                        if str(existing_path.resolve()).startswith(str(_downloads.DOWNLOADS_PATH.resolve())):
                            token = secrets.token_urlsafe(32)
                            with get_db() as conn:
                                cur = conn.cursor()
                                cur.execute("""
                                    INSERT INTO download_tokens (token, job_id, stack_name, archive_path, is_folder, expires_at)
                                    VALUES (%s, %s, %s, %s, %s, %s);
                                """, (token, job_id, stack_name, str(existing_path), False, expires_at))
                                conn.commit()
                            base_url = _get_base_url()
                            download_url = f"{base_url}/download/{token}"
                            return jsonify({'success': True, 'download_url': download_url, 'token': token, 'is_folder': False})
                        else:
                            new_name = f"{utils.local_now().strftime('%Y%m%d_%H%M%S')}_download_{utils.filename_safe(existing_path.name)}{existing_path.suffix}"
                            dest = _downloads.DOWNLOADS_PATH / new_name
                            try:
                                shutil.copy2(str(existing_path), str(dest))
                                token = secrets.token_urlsafe(32)
                                with get_db() as conn:
                                    cur = conn.cursor()
                                    cur.execute("""
                                        INSERT INTO download_tokens (token, job_id, stack_name, archive_path, is_folder, expires_at)
                                        VALUES (%s, %s, %s, %s, %s, %s);
                                    """, (token, job_id, stack_name, str(dest), False, expires_at))
                                    conn.commit()
                                base_url = _get_base_url()
                                download_url = f"{base_url}/download/{token}"
                                return jsonify({'success': True, 'download_url': download_url, 'token': token, 'is_folder': False})
                            except Exception:
                                # If copy fails, fall back to starting preparation
                                pass
                except Exception:
                    pass

            # First check for existing preparing token for same job/stack
            try:
                with get_db() as conn2:
                    cur2 = conn2.cursor()
                    cur2.execute("""
                        SELECT token, is_preparing FROM download_tokens
                        WHERE job_id = %s AND stack_name = %s AND is_preparing = true AND expires_at > NOW()
                        LIMIT 1;
                    """, (job_id, stack_name))
                    existing = cur2.fetchone()
                    if existing:
                        # Return existing token so clients reuse it instead of starting a new job
                        return jsonify({
                            'success': True,
                            'message': 'An archive is already being prepared for this stack',
                            'is_folder': True,
                            'is_preparing': True,
                            'token': existing['token']
                        })
            except Exception:
                # If the check fails, continue and attempt to create a new preparing token
                pass

            # No existing preparing token found; attempt to create a new token and set is_preparing=true
            token = secrets.token_urlsafe(32)
            try:
                with get_db() as conn3:
                    cur3 = conn3.cursor()
                    # Insert token already flagged as preparing; a unique partial index on (job_id, stack_name) WHERE is_preparing
                    # (created by DB migrations) will prevent a second preparing token being created concurrently.
                    cur3.execute("""
                        INSERT INTO download_tokens (token, job_id, stack_name, archive_path, is_folder, expires_at, is_preparing)
                        VALUES (%s, %s, %s, %s, %s, %s, true);
                    """, (token, job_id, stack_name, archive_path, is_folder, expires_at))
                    conn3.commit()
                    started_token = token
            except Exception:
                # Likely a race with another request inserting a preparing token; fetch it and return it
                try:
                    with get_db() as conn4:
                        cur4 = conn4.cursor()
                        cur4.execute("""
                            SELECT token FROM download_tokens
                            WHERE job_id = %s AND stack_name = %s AND is_preparing = true AND expires_at > NOW()
                            LIMIT 1;
                        """, (job_id, stack_name))
                        r = cur4.fetchone()
                        if r:
                            return jsonify({
                                'success': True,
                                'message': 'An archive is already being prepared for this stack',
                                'is_folder': True,
                                'is_preparing': True,
                                'token': r['token']
                            })
                except Exception:
                    pass

                # If we could not find an existing preparing token, fall back to creating a token without preparing flag
                token = secrets.token_urlsafe(32)
                with get_db() as conn5:
                    cur5 = conn5.cursor()
                    cur5.execute("""
                        INSERT INTO download_tokens (token, job_id, stack_name, archive_path, is_folder, expires_at)
                        VALUES (%s, %s, %s, %s, %s, %s);
                    """, (token, job_id, stack_name, archive_path, is_folder, expires_at))
                    conn5.commit()
                started_token = token

            # Start background compression using the token that was marked as preparing
            threading.Thread(
                target=_prepare_folder_download,
                args=(started_token, archive_path, stack_name, get_current_user()['email']),
                daemon=True
            ).start()

            return jsonify({
                'success': True,
                'message': 'Archive is being prepared. You will receive a notification when ready.',
                'is_folder': True,
                'is_preparing': True,
                'token': started_token
            })
        else:
            # Not a folder: insert a token and return immediate download link
            token = secrets.token_urlsafe(32)
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO download_tokens (token, job_id, stack_name, archive_path, is_folder, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s);
                """, (token, job_id, stack_name, archive_path, is_folder, expires_at))
                conn.commit()

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
    finally:
        # Clear is_preparing flag so subsequent requests will attempt fresh regenerations if needed
        try:
            with get_db() as conn4:
                cur4 = conn4.cursor()
                cur4.execute("UPDATE download_tokens SET is_preparing = false WHERE token = %s;", (token,))
                conn4.commit()
        except Exception:
            pass

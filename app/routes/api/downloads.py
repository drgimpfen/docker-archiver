"""
Download tokens API for secure stack archive downloads.
"""
import os
import uuid
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import request, jsonify, send_file
from app.routes.api import bp, api_auth_required
from app.db import get_db
from app.utils import get_downloads_path, get_logger, setup_logging
from app.notifications.adapters.smtp import SMTPAdapter
from app.notifications.core import get_setting

# Module-level locks to avoid concurrent packing for the same archive_path
_packing_locks: dict[str, threading.Lock] = {}
_packing_locks_lock = threading.Lock()  # guard structure mutations
from app import utils

# Configure logging
setup_logging()
logger = get_logger(__name__)


def generate_token():
    """Generate a secure UUID token."""
    return str(uuid.uuid4())


# Note: expired-download cleanup moved to `app.cleanup.cleanup_download_tokens` for single-responsibility. Wrapper removed.


def resume_pending_downloads(generate_missing: bool = False):
    """Resume any pending background packing tasks left in the database.

    If `generate_missing` is True, also look for tokens that are not marked
    `is_packing` but reference an existing archive directory and have no valid
    `file_path` on disk, and start packing for them.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT token, stack_name, file_path, archive_path FROM download_tokens
                WHERE is_packing = TRUE AND expires_at > NOW();
            """)
            rows = cur.fetchall()
    except Exception as e:
        logger.exception("Failed to query pending download tokens on startup: %s", e)
        return

    for r in (rows or []):
        try:
            token = r['token']
            stack_name = r['stack_name']
            archive_path = r.get('archive_path') or r.get('file_path')
            if not archive_path:
                continue
            p = Path(archive_path)
            # Only resume if source directory still exists
            if p.exists() and p.is_dir():
                # If there is already a completed archive for this archive_path, remove the stale token
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT token, file_path, is_packing FROM download_tokens
                        WHERE archive_path = %s AND expires_at > NOW() ORDER BY created_at DESC LIMIT 1;
                    """, (archive_path,))
                    existing = cur.fetchone()
                if existing and existing.get('file_path') and Path(existing.get('file_path')).exists() and not existing.get('is_packing'):
                    # Another token already produced the archive — remove this stale token
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("DELETE FROM download_tokens WHERE token = %s;", (token,))
                        conn.commit()
                    logger.info("Removed stale packing token %s (archive already exists)", token)
                    continue

                logger.info("Resuming packing for token %s (stack=%s, path=%s)", token, stack_name, archive_path)
                thread = threading.Thread(target=process_directory_pack, args=(stack_name, archive_path, token))
                thread.daemon = True
                thread.start()
            else:
                # Source no longer exists — remove token to avoid confusion
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM download_tokens WHERE token = %s;", (token,))
                    conn.commit()
                logger.info("Removed pending token %s because source path no longer exists", token)
        except Exception as e:
            logger.exception("Failed while resuming token %s: %s", r.get('token'), e)

    # Optionally start packing for non-packing tokens that reference existing directories
    if generate_missing:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT token, stack_name, file_path, archive_path, is_packing FROM download_tokens
                    WHERE expires_at > NOW() AND (is_packing = FALSE OR is_packing IS NULL)
                    ORDER BY created_at DESC;
                """)
                candidates = cur.fetchall()
        except Exception as e:
            logger.exception("Failed to query candidate download tokens for generation: %s", e)
            return

        for c in (candidates or []):
            try:
                token = c['token']
                stack_name = c.get('stack_name')
                file_path = c.get('file_path')
                archive_path = c.get('archive_path')

                # Skip if file already exists
                if file_path and Path(file_path).exists():
                    continue

                if not archive_path:
                    continue

                p = Path(archive_path)
                # Only start if source dir exists and is a directory
                if not (p.exists() and p.is_dir()):
                    continue

                # Atomically set is_packing to TRUE only if it was FALSE/NULL
                try:
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("UPDATE download_tokens SET is_packing = TRUE WHERE token = %s AND (is_packing = FALSE OR is_packing IS NULL) RETURNING token;", (token,))
                        r = cur.fetchone()
                        if r:
                            conn.commit()
                            logger.info("Starting generation for missing download token %s (stack=%s, path=%s)", token, stack_name, archive_path)
                            thread = threading.Thread(target=process_directory_pack, args=(stack_name, archive_path, token))
                            thread.daemon = True
                            thread.start()
                        else:
                            # somebody else started packing, ignore
                            continue
                except Exception as e:
                    logger.exception("Failed to atomically set packing flag for candidate token %s: %s", token, e)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            except Exception as e:
                logger.exception("Error while attempting to generate missing download for token %s: %s", c.get('token'), e)


def send_download_email(stack_name, download_url=None, recipients=None):
    """Send notification email with a download link to `recipients` (or default recipients if None)."""
    try:
        adapter = SMTPAdapter()
        title = f"Stack Archive Ready: {stack_name}"

        url_html = f"<p><a href=\"{download_url}\" class=\"btn btn-primary\">Download Archive</a></p>" if download_url else "<p>Your requested archive is ready.</p>"
        body = f"""
        <p>Your requested stack archive for <strong>{stack_name}</strong> is ready.</p>

        {url_html}

        <p><small>This link will expire in 24 hours.</small></p>

        <p>Best regards,<br>Docker Archiver</p>
        """

        result = adapter.send(title, body, recipients=recipients)
        if result.success:
            logger.info(f"Download notification email sent for stack {stack_name} to {recipients if recipients else 'default recipients'}")
        else:
            logger.error(f"Failed to send download email for stack {stack_name}: {result.detail}")

    except Exception as e:
        logger.exception(f"Error sending download email for stack {stack_name}: {e}")


def _get_source_timestamp(source_path: str) -> str:
    """Return a timestamp string based on the source path's latest mtime.

    For files, use the file mtime. For directories, walk a limited number of
    files to find the newest mtime to reflect the actual content timestamp.
    """
    try:
        p = Path(source_path)
        latest = time.time()
        if p.exists():
            if p.is_file():
                latest = p.stat().st_mtime
            else:
                # Walk directory to find latest mtime; cap to avoid scanning huge trees
                latest = p.stat().st_mtime
                count = 0
                for root, dirs, files in os.walk(p):
                    for fname in files:
                        count += 1
                        if count > 5000:
                            break
                        try:
                            t = os.path.getmtime(os.path.join(root, fname))
                            if t > latest:
                                latest = t
                        except Exception:
                            continue
                    if count > 5000:
                        break
        dt = datetime.fromtimestamp(latest, tz=timezone.utc).astimezone(utils.get_display_timezone())
        return dt.strftime('%Y%m%d_%H%M%S')
    except Exception:
        return utils.local_now().strftime('%Y%m%d_%H%M%S')


def pack_stack_directory(stack_name, source_path, output_path):
    """Pack a stack directory into a tar.zst archive using zstd for compression.

    Uses multi-threaded zstd for faster compression (`zstd -T0 -19`) via tar's
    `-I`/`--use-compress-program` option. Requires `zstd` to be available in the
    container (the Dockerfile already installs it).
    """
    try:
        logger.info(f"Starting to pack stack {stack_name} from {source_path} to {output_path}")

        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Use tar with zstd compression (multi-threaded, reasonable level)
        cmd = ['tar', '-I', 'zstd -T0 -19', '-cf', str(output_path), '-C', str(Path(source_path).parent), Path(source_path).name]

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
    """Background process to pack directory and send email. Ensures only one pack per
    archive_path runs at a time by acquiring a per-archive lock.
    """
    lock = None
    try:
        # Acquire per-archive lock
        with _packing_locks_lock:
            lock = _packing_locks.get(source_path)
            if not lock:
                lock = threading.Lock()
                _packing_locks[source_path] = lock
        acquired = lock.acquire(blocking=False)
        if not acquired:
            logger.info("Another packing job is already running for %s, skipping", source_path)
            return

        downloads_path = get_downloads_path()
        ts = _get_source_timestamp(source_path)
        output_filename = f"{stack_name}_{ts}.tar.zst"
        output_path = downloads_path / output_filename

        # If output already exists, reuse it and update DB
        if output_path.exists():
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE download_tokens SET file_path = %s, is_packing = FALSE WHERE token = %s;
                    """, (str(output_path), token))
                    conn.commit()
                base_url = get_setting('base_url', 'http://localhost:8080')
                download_url = f"{base_url}/download/{token}"
                # send to per-token notify_emails if present
                with get_db() as conn:
                    c = conn.cursor()
                    c.execute("SELECT notify_emails FROM download_tokens WHERE token = %s;", (token,))
                    n = c.fetchone()
                    notify_list = n.get('notify_emails') if n else None
                send_download_email(stack_name, download_url, recipients=notify_list if notify_list else None)
                logger.info("Reused existing archive for token %s at %s", token, output_path)
                return
            except Exception as e:
                logger.exception("Failed to reuse existing output for %s: %s", output_path, e)

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

            # Send email with download link to notify_emails if present
            with get_db() as conn:
                c = conn.cursor()
                c.execute("SELECT notify_emails FROM download_tokens WHERE token = %s;", (token,))
                n = c.fetchone()
                notify_list = n.get('notify_emails') if n else None

            base_url = get_setting('base_url', 'http://localhost:8080')
            download_url = f"{base_url}/download/{token}"
            send_download_email(stack_name, download_url, recipients=notify_list if notify_list else None)

            try:
                if output_path.exists():
                    logger.info(f"Packed archive size: {output_path.stat().st_size} bytes for token {token}")
            except Exception:
                pass
        else:
            # Packing failed, remove token
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM download_tokens WHERE token = %s;", (token,))
                    conn.commit()
                logger.error(f"Packing failed for stack {stack_name}, token removed")
            except Exception:
                pass
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
    finally:
        try:
            if lock and lock.locked():
                lock.release()
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
    expires_at = utils.now() + timedelta(hours=24)
    
    try:
        with get_db() as conn:
            cur = conn.cursor()
            
            if path.is_file():
                # File exists, create token immediately and send email
                notify_input = data.get('notify_email') if isinstance(data, dict) else None
                emails = None
                if notify_input and isinstance(notify_input, str):
                    emails = [e.strip() for e in notify_input.split(',') if e.strip()]
                cur.execute("""
                    INSERT INTO download_tokens (token, stack_name, file_path, archive_path, expires_at, is_packing, notify_emails)
                    VALUES (%s, %s, %s, %s, %s, FALSE, %s);
                """, (token, stack_name, archive_path, archive_path, expires_at, emails))
                conn.commit()
                
                # Send email immediately with download link
                base_url = get_setting('base_url', 'http://localhost:8080')
                download_url = f"{base_url}/download/{token}"
                try:
                    send_download_email(stack_name, download_url=download_url, recipients=emails)
                except Exception:
                    logger.exception('Failed to send download email')

                return jsonify({
                    'success': True,
                    'message': 'Download email sent',
                    'is_folder': False,
                    'token': token
                })
                
            elif path.is_dir():
                # Directory needs packing
                # First check if we already have an active token for this archive_path
                cur.execute("""
                    SELECT token, file_path, expires_at, is_packing FROM download_tokens
                    WHERE archive_path = %s AND expires_at > NOW()
                    ORDER BY created_at DESC LIMIT 1;
                """, (archive_path,))
                existing = cur.fetchone()
                base_url = get_setting('base_url', 'http://localhost:8080')
                if existing:
                    # If an existing archive is already prepared and file exists, reuse it
                    if existing.get('file_path') and Path(existing.get('file_path')).exists() and not existing.get('is_packing'):
                        # Send the existing archive as an email attachment to default recipients
                        try:
                            # fetch any notify_emails associated with the existing token
                            with get_db() as nconn:
                                nc = nconn.cursor()
                                nc.execute("SELECT notify_emails FROM download_tokens WHERE token = %s;", (existing['token'],))
                                nrow = nc.fetchone()
                                notify_list = nrow.get('notify_emails') if nrow else None
                            download_url = f"{get_setting('base_url', 'http://localhost:8080')}/download/{existing['token']}"
                            send_download_email(stack_name, download_url=download_url, recipients=notify_list)
                        except Exception:
                            logger.exception('Failed to send existing archive as notification')
                        return jsonify({'success': True, 'message': 'Existing archive found; notification sent', 'is_folder': False, 'token': existing['token']})
                    # If it's currently packing, just return token so UI will poll
                    if existing.get('is_packing'):
                        return jsonify({'success': True, 'message': 'Archive preparation already started', 'is_folder': True, 'token': existing['token']})

                # No existing ready archive — insert token and mark as packing
                # Insert notify_input if provided
                notify_input = data.get('notify_email') if isinstance(data, dict) else None
                # Insert token and save notify_emails if provided (comma-separated)
                emails = None
                if notify_input and isinstance(notify_input, str):
                    emails = [e.strip() for e in notify_input.split(',') if e.strip()]
                cur.execute("""
                    INSERT INTO download_tokens (token, stack_name, file_path, archive_path, expires_at, is_packing, notify_emails)
                    VALUES (%s, %s, %s, %s, %s, TRUE, %s);
                """, (token, stack_name, archive_path, archive_path, expires_at, emails))
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
                    'is_folder': True,
                    'token': token
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
                SELECT token, stack_name, created_at, expires_at, is_packing, file_path, notify_emails
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
                'is_packing': t['is_packing'],
                'file_path': t.get('file_path'),
                'file_name': (Path(t.get('file_path')).name if t.get('file_path') else None),
                'notify_emails': t.get('notify_emails') or []
            } for t in tokens]
        })
        
    except Exception as e:
        logger.exception("Error listing download tokens")
        return jsonify({'error': 'Internal server error'}), 500


@bp.route('/downloads/status')
@api_auth_required
def download_status():
    """Return the status for a preparing download token."""
    token = request.args.get('token')
    if not token:
        return jsonify({'error': 'Missing token'}), 400
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT file_path, archive_path, expires_at, is_packing, stack_name FROM download_tokens WHERE token = %s;
            """, (token,))
            row = cur.fetchone()
            if not row:
                return jsonify({'ready': False, 'error': 'Token not found or expired'}), 404
            # Check expiry (normalize to UTC)
            expires_at = utils.ensure_utc(row.get('expires_at'))
            if expires_at and expires_at < utils.now():
                return jsonify({'ready': False, 'error': 'Token expired'}), 404
            fp = row.get('file_path')
            # If file exists and packing is finished, respond ready
            if fp and Path(fp).exists() and not row.get('is_packing'):
                base = get_setting('base_url', 'http://localhost:8080')
                return jsonify({'ready': True, 'download_url': f"{base}/download/{token}"})
            else:
                # As a convenience, if the archive_path corresponds to an already existing archive
                # (created by a previous run into /archives), proactively mark as ready and return download URL
                ap = row.get('archive_path')
                if ap and Path(ap).exists() and Path(ap).is_file() and not row.get('is_packing'):
                    base = get_setting('base_url', 'http://localhost:8080')
                    return jsonify({'ready': True, 'download_url': f"{base}/download/{token}"})
                return jsonify({'ready': False, 'is_packing': row.get('is_packing', False)})
    except Exception as e:
        logger.exception('Error checking download status')
        return jsonify({'error': 'Internal server error'}), 500


@bp.route('/downloads/send_link', methods=['POST'])
@api_auth_required
def send_link():
    """Deprecated for UI (keeps backward compatibility) — prefer using public form which routes to the same logic.

    This endpoint still accepts JSON { token, email } or { archive_path, email }.
    """
    """Request the server to send a download link to a specific email address.

    Body accepts either { token, email } or { archive_path, email }.
    If the token exists and the archive is ready the email is sent immediately.
    If the archive is still packing, the token's `notify_emails` will be updated so
    the background pack job will notify the provided addresses when ready.
    If no token exists for an archive_path, a token is created (and packing
    started) with `notify_emails` set so the recipients are notified when ready.
    """
    data = request.get_json() or {}
    email = (data.get('email') or '').strip() if isinstance(data, dict) else ''
    token = data.get('token') if isinstance(data, dict) else None
    archive_path = data.get('archive_path') if isinstance(data, dict) else None

    if not email:
        return jsonify({'error': 'Missing email address'}), 400

    try:
        with get_db() as conn:
            cur = conn.cursor()
            if token:
                cur.execute("SELECT token, file_path, archive_path, is_packing, stack_name FROM download_tokens WHERE token = %s;", (token,))
                row = cur.fetchone()
                if not row:
                    return jsonify({'error': 'Token not found'}), 404

                # If ready, send immediately
                if row.get('file_path') and Path(row.get('file_path')).exists() and not row.get('is_packing'):
                    download_url = f"{get_setting('base_url', 'http://localhost:8080')}/download/{token}"
                    send_download_email(row.get('stack_name'), download_url, recipients=[email])
                    return jsonify({'success': True, 'message': 'Email sent'}), 200

                # Update notify_emails ensuring it's a unique set (merge existing with provided list)
                emails = [e.strip() for e in email.split(',') if e.strip()]
                if emails:
                    cur.execute("""
                        UPDATE download_tokens
                        SET notify_emails = (
                            SELECT ARRAY(SELECT DISTINCT e FROM (
                                SELECT unnest(COALESCE(notify_emails, ARRAY[]::text[])) UNION ALL SELECT unnest(%s::text[])
                            ) AS e)
                        )
                        WHERE token = %s;
                    """, (emails, token))
                    conn.commit()

                # If file missing and not currently packing, start regeneration (useful when user requested via public page)
                if (not row.get('file_path') or not Path(row.get('file_path')).exists()) and not row.get('is_packing'):
                    # Only start if archive_path looks like a directory or exists
                    ap = row.get('archive_path')
                    if ap and Path(ap).exists():
                        # atomically set is_packing to TRUE only if it was FALSE/NULL
                        try:
                            cur.execute("UPDATE download_tokens SET is_packing = TRUE WHERE token = %s AND (is_packing = FALSE OR is_packing IS NULL) RETURNING token;", (token,))
                            r = cur.fetchone()
                            if r:
                                conn.commit()
                                thread = threading.Thread(target=process_directory_pack, args=(row.get('stack_name'), ap, token))
                                thread.daemon = True
                                thread.start()
                            else:
                                # someone else started packing concurrently; return 202
                                return jsonify({'success': True, 'message': 'Archive preparation already started', 'is_folder': True, 'token': token}), 202
                        except Exception as e:
                            logger.exception('Failed to atomically set packing flag for token %s: %s', token, e)
                            try:
                                conn.rollback()
                            except Exception:
                                pass

                return jsonify({'success': True, 'message': 'Recipient saved; will be notified when ready.'}), 202

            elif archive_path:
                # Find any existing token for this archive_path
                cur.execute("SELECT token, file_path, is_packing, stack_name FROM download_tokens WHERE archive_path = %s ORDER BY created_at DESC LIMIT 1;", (archive_path,))
                row = cur.fetchone()
                tkn = None
                if row:
                    tkn = row.get('token')
                    # If an existing archive file is already present and not packing, create a NEW token and send it
                    if row.get('file_path') and Path(row.get('file_path')).exists() and not row.get('is_packing'):
                        new_tkn = generate_token()
                        expires_at = utils.now() + timedelta(hours=24)
                        emails = [e.strip() for e in (email or '').split(',') if e.strip()]
                        # Insert a fresh token that points to the existing file
                        cur.execute("INSERT INTO download_tokens (token, stack_name, file_path, archive_path, expires_at, is_packing, notify_emails) VALUES (%s, %s, %s, %s, %s, FALSE, %s);", (new_tkn, row.get('stack_name') or data.get('stack_name') or 'unknown', row.get('file_path'), archive_path, expires_at, emails))
                        conn.commit()
                        download_url = f"{get_setting('base_url', 'http://localhost:8080')}/download/{new_tkn}"
                        try:
                            send_download_email(row.get('stack_name'), download_url, recipients=emails)
                        except Exception:
                            logger.exception('Failed to send download email for new token')
                        return jsonify({'success': True, 'message': 'New token created and email sent', 'is_folder': False, 'download_url': download_url, 'token': new_tkn}), 200
                    # If packing, update notify_emails
                    emails = [e.strip() for e in (email or '').split(',') if e.strip()]
                    if emails:
                        cur.execute("""
                            UPDATE download_tokens
                            SET notify_emails = (
                                SELECT ARRAY(SELECT DISTINCT e FROM (
                                    SELECT unnest(COALESCE(notify_emails, ARRAY[]::text[])) UNION ALL SELECT unnest(%s::text[])
                                ) AS e)
                            )
                            WHERE token = %s;
                        """, (emails, tkn))
                        conn.commit()
                    # If not currently packing, try starting packing
                    if not row.get('is_packing'):
                        try:
                            cur.execute("UPDATE download_tokens SET is_packing = TRUE WHERE token = %s;", (tkn,))
                            conn.commit()
                            thread = threading.Thread(target=process_directory_pack, args=(row.get('stack_name') or data.get('stack_name') or 'unknown', archive_path, tkn))
                            thread.daemon = True
                            thread.start()
                        except Exception:
                            pass
                    return jsonify({'success': True, 'message': 'Recipient saved; will be notified when ready.'}), 202

                # No existing token: create one and start packing
                tkn = generate_token()
                expires_at = utils.now() + timedelta(hours=24)
                emails = [e.strip() for e in (email or '').split(',') if e.strip()]
                cur.execute("INSERT INTO download_tokens (token, stack_name, file_path, archive_path, expires_at, is_packing, notify_emails) VALUES (%s, %s, %s, %s, %s, TRUE, %s);", (tkn, data.get('stack_name') or 'unknown', archive_path, archive_path, expires_at, emails))
                conn.commit()
                thread = threading.Thread(target=process_directory_pack, args=(data.get('stack_name') or 'unknown', archive_path, tkn))
                thread.daemon = True
                thread.start()
                return jsonify({'success': True, 'message': 'Packing started and recipient will be notified when ready.'}), 202

            else:
                return jsonify({'error': 'Missing token or archive_path'}), 400
    except Exception as e:
        logger.exception('Error sending link: %s', e)
        return jsonify({'error': 'Internal server error'}), 500


@bp.route('/downloads/tokens/<token>', methods=['DELETE'])
@api_auth_required
def delete_token(token):
    """Delete a download token and any associated temporary files (if safe)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT file_path FROM download_tokens WHERE token = %s;", (token,))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': 'Token not found'}), 404
            fp = row.get('file_path')
            cur.execute("DELETE FROM download_tokens WHERE token = %s;", (token,))
            conn.commit()
        # If the file was under /tmp/downloads, remove it
        if fp and fp.startswith('/tmp/downloads'):
            try:
                os.remove(fp)
                logger.info('Removed temporary archive file %s after token deletion', fp)
            except Exception:
                logger.exception('Failed to remove temporary archive file %s', fp)
        return jsonify({'success': True}), 200
    except Exception as e:
        logger.exception('Failed to delete token %s: %s', token, e)
        return jsonify({'error': 'Internal server error'}), 500




"""
API routes for JSON endpoints.
Supports both session-based auth (for web UI) and API token auth (for external calls).
"""
import secrets
import os
import subprocess
import threading
from functools import wraps
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, request, jsonify, send_file, Response, stream_with_context
from app.auth import login_required, get_current_user
from app.db import get_db
from app import utils


bp = Blueprint('api', __name__, url_prefix='/api')


def api_auth_required(f):
    """
    Decorator for API endpoints that accepts both session auth and API token auth.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check for API token in Authorization header
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header[7:]  # Remove 'Bearer ' prefix
            
            # Validate token
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT u.* FROM users u
                    JOIN api_tokens t ON u.id = t.user_id
                    WHERE t.token = %s AND (t.expires_at IS NULL OR t.expires_at > NOW());
                """, (token,))
                user = cur.fetchone()
            
            if user:
                # Token is valid, proceed
                return f(*args, **kwargs)
            else:
                return jsonify({'error': 'Invalid or expired API token'}), 401
        
        # Fall back to session auth (for web UI)
        return login_required(f)(*args, **kwargs)
    
    return decorated_function


@bp.route('/jobs/<int:job_id>')
@api_auth_required
def get_job_details(job_id):
    """Get job details (for modal)."""
    with get_db() as conn:
        cur = conn.cursor()
        
        # Get job
        cur.execute("""
            SELECT j.*, 
                   a.name as archive_name,
                   EXTRACT(EPOCH FROM (j.end_time - j.start_time))::INTEGER as duration_seconds
            FROM jobs j
            LEFT JOIN archives a ON j.archive_id = a.id
            WHERE j.id = %s;
        """, (job_id,))
        job = cur.fetchone()
        
        if not job:
            return jsonify({'error': 'Job not found'}), 404
        
        # Get stack metrics
        cur.execute("""
            SELECT * FROM job_stack_metrics 
            WHERE job_id = %s 
            ORDER BY start_time;
        """, (job_id,))
        metrics = cur.fetchall()
        
        # Set file_exists based on deleted_at timestamp
        for metric in metrics:
            if metric.get('archive_path'):
                metric['file_exists'] = metric.get('deleted_at') is None
            else:
                metric['file_exists'] = None
    
    job_out = dict(job)
    for k in ('start_time', 'end_time'):
        if job_out.get(k):
            job_out[k] = utils.to_iso_z(job_out[k])

    metrics_out = []
    for m in metrics:
        md = dict(m)
        for key, val in list(md.items()):
            # convert any datetime-like fields
            if hasattr(val, 'astimezone'):
                md[key] = utils.to_iso_z(val)
        metrics_out.append(md)

    return jsonify({
        'job': job_out,
        'metrics': metrics_out
    })


@bp.route('/jobs/<int:job_id>/download', methods=['POST'])
@api_auth_required
def request_download(job_id):
    """Request download for an archive (generates token and prepares file)."""
    try:
        data = request.get_json()
        stack_name = data.get('stack_name')
        archive_path = data.get('archive_path')
        
        # Allow archive_path to be absolute or relative to ARCHIVES_PATH; normalize and check both
        candidate_paths = []
        if archive_path:
            candidate_paths.append(archive_path)
            # If archive_path looks relative or doesn't exist, try under ARCHIVES_PATH
            archives_root = os.environ.get('ARCHIVES_PATH', '/archives')
            candidate_paths.append(os.path.join(archives_root, archive_path.lstrip('/')))
            # Also try realpath of each
            candidate_paths = [os.path.realpath(p) for p in candidate_paths]

        found_path = None
        for p in candidate_paths:
            try:
                if p and os.path.exists(p):
                    found_path = p
                    break
            except Exception:
                continue

        if not archive_path or not found_path:
            return jsonify({'error': 'Archive not found'}), 404

        # Use the resolved absolute path
        archive_path = found_path
        
        # Check if it's a folder - if yes, we need to create an archive
        is_folder = os.path.isdir(archive_path)
        
        # Check for an existing valid token for this job/stack so we don't create duplicate work
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT token, archive_path, is_folder, is_preparing FROM download_tokens
                WHERE job_id = %s AND stack_name = %s AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY id DESC LIMIT 1;
            """, (job_id, stack_name))
            existing = cur.fetchone()

        if existing:
            # If a ready file already exists for the token, return its download URL
            existing_path = existing.get('archive_path')
            if existing_path and os.path.exists(existing_path):
                base_url = _get_base_url().rstrip('/')
                download_url = f"{base_url}/download/{existing['token']}"
                return jsonify({
                    'success': True,
                    'download_url': download_url,
                    'token': existing['token'],
                    'expires_in': '24 hours',
                    'is_folder': False
                })
            # If it's actively preparing, inform the caller so the UI can show the modal
            if existing.get('is_preparing'):
                return jsonify({
                    'success': True,
                    'message': 'Archive is being prepared. You will receive a notification when it is ready.',
                    'is_folder': True,
                    'is_preparing': True,
                    'token': existing['token']
                })

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
            # If the requested file path does not (yet) exist on disk, start background regeneration
            if archive_path and not os.path.exists(archive_path):
                try:
                    from app.downloads import regenerate_token
                    threading.Thread(target=regenerate_token, args=(token,), daemon=True).start()
                    return jsonify({
                        'success': True,
                        'message': 'The file is being prepared; please try again in a few minutes.',
                        'is_folder': False,
                        'is_preparing': True,
                        'token': token
                    })
                except Exception as e:
                    print(f"[API] Failed to start background regeneration for token {token}: {e}")

            # File is ready (or we couldn't start regen), return download link
            base_url = _get_base_url().rstrip('/')
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


@bp.route('/jobs/<int:job_id>/log')
@api_auth_required
def download_log(job_id):
    """Download job log as text file. Optionally filter by stack name using ?stack=stackname query parameter."""
    stack_name = request.args.get('stack')
    
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT j.log, a.name as archive_name, j.status FROM jobs j LEFT JOIN archives a ON j.archive_id = a.id WHERE j.id = %s;", (job_id,))
        result = cur.fetchone()
    
    if not result or not result['log']:
        return "No log available", 404
    
    log_content = result['log']
    
    # Filter log by stack if requested
    if stack_name:
        lines = log_content.split('\n')
        filtered_lines = []
        in_stack_section = False
        
        for line in lines:
            # Check if we're entering the requested stack section
            if f"--- Starting backup for stack: {stack_name} ---" in line:
                in_stack_section = True
            # Check if we're finishing this stack section
            elif f"--- Finished backup for stack: {stack_name} ---" in line:
                filtered_lines.append(line)
                in_stack_section = False
            # Check if we're entering a different stack section
            elif "--- Starting backup for stack:" in line and f"stack: {stack_name} ---" not in line:
                in_stack_section = False
            
            if in_stack_section:
                filtered_lines.append(line)
        
        log_content = '\n'.join(filtered_lines)
        if not log_content.strip():
            return f"No log entries found for stack '{stack_name}'", 404
    
    # Create temporary log file
    filename_suffix = f"_{stack_name}" if stack_name else ""
    log_filename = f"job_{job_id}_{result['archive_name'] or 'unknown'}{filename_suffix}.log"
    log_path = f"/tmp/{log_filename}"
    
    with open(log_path, 'w') as f:
        f.write(log_content)
    
    return send_file(log_path, as_attachment=True, download_name=log_filename)




@bp.route('/jobs/<int:job_id>/events')
@api_auth_required
def job_events(job_id):
    """SSE endpoint for job events. Streams JSON messages of the form:
    {"type": "log" | "status" | "metrics", "data": {...}}
    """
    try:
        from app.sse import register_event_listener, unregister_event_listener
    except Exception:
        return jsonify({'error': 'SSE not available in this deployment'}), 501

    def gen():
        q = register_event_listener(job_id)
        try:
            # Send initial keep-alive comment
            yield ': connected\n\n'
            while True:
                try:
                    msg = q.get(timeout=15)
                except Exception:
                    # keepalive
                    yield ': keepalive\n\n'
                    continue
                # Send as raw data (client will JSON-parse)
                yield f"data: {msg}\n\n"
        finally:
            unregister_event_listener(job_id, q)

    return Response(stream_with_context(gen()), mimetype='text/event-stream')


# Global Jobs SSE endpoint removed (dashboard uses polling now).


# Debug endpoint for SSE internals (secured by api_auth_required)
@bp.route('/_debug/sse')
@api_auth_required
def debug_sse():
    try:
        from app.sse import get_status
        return jsonify({'sse': get_status()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/jobs/<int:job_id>/log/tail')
@api_auth_required
def tail_log(job_id):
    """Return incremental log lines for a job. Query params: last_line (int, default=0), stack (optional filter).

    Uses running ArchiveExecutor's in-memory buffer when available for live logs, otherwise falls back to stored job.log in the DB.
    Returns JSON: {lines: [...], last_line: <int>, complete: <bool>}"""
    last_line = request.args.get('last_line', type=int, default=0)
    stack_name = request.args.get('stack')

    # Try to get live executor
    try:
        from app.executor import get_running_executor
        executor = get_running_executor(job_id)
    except Exception:
        executor = None

    lines = []
    complete = True

    if executor:
        # Merge DB-stored log and in-memory buffer for robust live-tail across workers.
        db_lines = []
        job_status = 'running'
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT j.log, j.status FROM jobs j WHERE j.id = %s;", (job_id,))
            result = cur.fetchone()
            if result and result.get('log'):
                db_lines = result['log'].split('\n')
                job_status = result.get('status') or job_status

        mem_lines = list(executor.log_buffer)

        # If DB appears to already contain all lines, prefer DB as authoritative
        if len(db_lines) >= len(mem_lines):
            lines = db_lines
        else:
            # If mem_lines starts with db_lines, append the remaining tail from mem_lines
            if db_lines and mem_lines[:len(db_lines)] == db_lines:
                lines = db_lines + mem_lines[len(db_lines):]
            else:
                # Best-effort merge: start with db_lines and append mem_lines while avoiding immediate duplicates
                merged = db_lines.copy()
                for l in mem_lines:
                    if not merged or merged[-1] != l:
                        merged.append(l)
                lines = merged

        # Job is still running when an executor exists (live logs)
        complete = False
    else:
        # Fallback: read stored log from DB
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT j.log, j.status FROM jobs j WHERE j.id = %s;", (job_id,))
            result = cur.fetchone()
            if not result or not result['log']:
                return jsonify({'lines': [], 'last_line': 0, 'complete': True})
            lines = result['log'].split('\n')
            complete = result.get('status') != 'running'

    # If stack filter is requested, filter to that stack section
    if stack_name:
        filtered = []
        in_stack_section = False
        for line in lines:
            if f"--- Starting backup for stack: {stack_name} ---" in line:
                in_stack_section = True
            elif f"--- Finished backup for stack: {stack_name} ---" in line:
                filtered.append(line)
                in_stack_section = False
            elif "--- Starting backup for stack:" in line and f"stack: {stack_name} ---" not in line:
                in_stack_section = False

            if in_stack_section:
                filtered.append(line)
        lines = filtered

    total_lines = len(lines)

    # Ensure last_line is not out of range
    if last_line < 0:
        last_line = 0
    if last_line > total_lines:
        last_line = total_lines

    new_lines = lines[last_line:]
    new_last_line = total_lines

    # Also return minimal job metadata so clients can update UI without an extra request
    job_meta = {}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, status, start_time, end_time, duration_seconds, total_size_bytes, reclaimed_bytes FROM jobs WHERE id = %s;", (job_id,))
        row = cur.fetchone()
        if row:
            job_meta = dict(row)

    return jsonify({
        'lines': new_lines,
        'last_line': new_last_line,
        'complete': complete,
        'job': job_meta
    })




def _prepare_folder_download(token, folder_path, stack_name, user_email):
    """Background task to compress folder and notify user."""
    try:
        # Create compressed archive in configured DOWNLOADS_PATH
        from app import downloads as _downloads
        try:
            _downloads.DOWNLOADS_PATH.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        timestamp = utils.local_now().strftime('%Y%m%d_%H%M%S')
        safe_name = utils.filename_safe(stack_name)
        archive_name = f"{timestamp}_download_{safe_name}.tar.zst"
        archive_path = _downloads.DOWNLOADS_PATH / archive_name

        # Mark token as preparing
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("UPDATE download_tokens SET is_preparing = true WHERE token = %s;", (token,))
                conn.commit()
        except Exception as e:
            print(f"[ERROR] Failed to mark token {token} as preparing: {e}")

        try:
            # Compress folder
            subprocess.run(
                ['tar', '-I', 'zstd', '-cf', str(archive_path), '-C', str(Path(folder_path).parent), Path(folder_path).name],
                check=True,
                timeout=3600
            )

            # Update token with new path and clear preparing flag
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE download_tokens 
                    SET archive_path = %s, is_folder = false, is_preparing = false 
                    WHERE token = %s;
                """, (str(archive_path), token))
                conn.commit()
            
            # Send notification
            if user_email:
                import apprise
                apobj = apprise.Apprise()
                
                # Get SMTP config
                smtp_server = os.environ.get('SMTP_SERVER')
                smtp_user = os.environ.get('SMTP_USER')
                smtp_password = os.environ.get('SMTP_PASSWORD')
                smtp_port = os.environ.get('SMTP_PORT', '587')
                smtp_from = os.environ.get('SMTP_FROM')
                
                if all([smtp_server, smtp_user, smtp_password, smtp_from]):
                    mailto_url = f"mailtos://{smtp_user}:{smtp_password}@{smtp_server}:{smtp_port}/?from={smtp_from}&to={user_email}"
                    apobj.add(mailto_url)
                    
                    base_url = _get_base_url().rstrip('/')
                    download_url = f"{base_url}/download/{token}"
                    
                    apobj.notify(
                        title="📦 Archive Download Ready",
                        body=f"""<h2>Your archive is ready for download</h2>
<p><strong>Stack:</strong> {stack_name}</p>
<p><a href="{download_url}">Download Archive</a></p>
<p><small>This link will expire in 24 hours</small></p>""",
                        body_format='html'
                    )
        except Exception as e:
            print(f"[ERROR] Failed to prepare download: {e}")
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("UPDATE download_tokens SET is_preparing = false WHERE token = %s;", (token,))
                    conn.commit()
            except Exception:
                pass
    except Exception as e:
        print(f"[ERROR] _prepare_folder_download failed: {e}")
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("UPDATE download_tokens SET is_preparing = false WHERE token = %s;", (token,))
                conn.commit()
        except Exception:
            pass


@bp.route('/archives', methods=['GET'])
@api_auth_required
def list_archives():
    """List all archives (API endpoint)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM archives ORDER BY name;")
        archives = cur.fetchall()
    
    return jsonify({
        'archives': [dict(a) for a in archives]
    })


@bp.route('/archives/<int:archive_id>/run', methods=['POST'])
@api_auth_required
def run_archive(archive_id):
    """Trigger archive execution (API endpoint)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM archives WHERE id = %s;", (archive_id,))
            archive = cur.fetchone()
        
        if not archive:
            return jsonify({'error': 'Archive not found'}), 404

        # Create a job record atomically to prevent race conditions where
        # two concurrent requests could both insert a running job.
        from app import utils as u
        from app.sse import send_global_event
        with get_db() as conn:
            cur = conn.cursor()
            start_time = u.now()
            cur.execute("""
                INSERT INTO jobs (archive_id, job_type, status, start_time, triggered_by, log)
                SELECT %s, 'archive', 'running', %s, 'api', ''
                WHERE NOT EXISTS (
                    SELECT 1 FROM jobs WHERE archive_id = %s AND status = 'running'
                )
                RETURNING id;
            """, (archive_id, start_time, archive_id))
            row = cur.fetchone()
            if not row:
                # Another running job exists
                return jsonify({'error': 'Archive already has a running job'}), 409
            job_id = row['id']
            conn.commit()

            # Publish a lightweight global job event so any connected dashboards
            # immediately see the new job (avoids needing a manual refresh)
            try:
                cur.execute("""
                    SELECT j.id, j.archive_id, j.job_type, j.status, j.start_time, a.name as archive_name
                    FROM jobs j
                    LEFT JOIN archives a ON j.archive_id = a.id
                    WHERE j.id = %s;
                """, (job_id,))
                job_row = cur.fetchone()
                if job_row:
                    payload = dict(job_row)
                    try:
                        send_global_event('job', payload)
                    except Exception:
                        # best-effort; do not fail the API if SSE publish fails
                        pass
            except Exception:
                # ignore any errors when trying to fetch/publish job summary
                pass

        # Start archive job in a detached subprocess and write stdout/stderr to a log
        import sys
        jobs_dir = os.environ.get('ARCHIVE_JOB_LOG_DIR', '/var/log/archiver')
        os.makedirs(jobs_dir, exist_ok=True)
        # Create a per-job timestamped log file and pass it to the subprocess via --log-path
        timestamp = utils.local_now().strftime('%Y%m%d_%H%M%S')
        safe_name = utils.filename_safe(archive['name'])
        log_name = f"{timestamp}_archive_{safe_name}.log"
        log_path = os.path.join(jobs_dir, log_name)

        cmd = [sys.executable, '-m', 'app.run_job', '--archive-id', str(archive_id), '--job-id', str(job_id), '--log-path', log_path]
        subprocess.Popen(cmd, start_new_session=True)

        return jsonify({
            'success': True,
            'message': f"Archive '{archive['name']}' started",
            'archive_id': archive_id,
            'job_id': job_id,
            'log_path': log_path
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/archives/<int:archive_id>/dry-run', methods=['POST'])
@api_auth_required
def dry_run_archive(archive_id):
    """Trigger dry run execution (API endpoint)."""
    try:
        data = request.get_json() or {}
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM archives WHERE id = %s;", (archive_id,))
            archive = cur.fetchone()
        
        if not archive:
            return jsonify({'error': 'Archive not found'}), 404
        
        # Dry run config
        dry_run_config = {
            'stop_containers': data.get('stop_containers', True),
            'create_archive': data.get('create_archive', True),
            'run_retention': data.get('run_retention', True)
        }
        
        # Start dry run in a detached subprocess and pass an explicit per-run log path
        import sys
        jobs_dir = os.environ.get('ARCHIVE_JOB_LOG_DIR', '/var/log/archiver')
        os.makedirs(jobs_dir, exist_ok=True)
        timestamp = utils.local_now().strftime('%Y%m%d_%H%M%S')
        safe_name = utils.filename_safe(archive['name'])
        log_name = f"{timestamp}_dryrun_{safe_name}.log"
        log_path = os.path.join(jobs_dir, log_name)

        cmd = [sys.executable, '-m', 'app.run_job', '--archive-id', str(archive_id), '--dry-run', '--log-path', log_path]
        # Pass negative flags if config disables the behavior
        if not dry_run_config.get('stop_containers', True):
            cmd.append('--no-stop-containers')
        if not dry_run_config.get('create_archive', True):
            cmd.append('--no-create-archive')
        if not dry_run_config.get('run_retention', True):
            cmd.append('--no-run-retention')

        with open(log_path, 'ab') as fh:
            subprocess.Popen(cmd, stdout=fh, stderr=fh, start_new_session=True)

        return jsonify({
            'success': True,
            'message': f"Dry run for '{archive['name']}' started",
            'archive_id': archive_id,
            'dry_run_config': dry_run_config,
            'log_path': log_path
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/stacks', methods=['GET'])
@api_auth_required
def list_stacks():
    """List all discovered stacks (API endpoint)."""
    from app.stacks import discover_stacks
    stacks = discover_stacks()
    
    return jsonify({
        'stacks': stacks
    })


@bp.route('/jobs', methods=['GET'])
@api_auth_required
def list_jobs():
    """List jobs with optional filters (API endpoint)."""
    archive_id = request.args.get('archive_id', type=int)
    job_type = request.args.get('type')
    limit = request.args.get('limit', type=int, default=20)
    
    # Return a lightweight summary for the jobs list to avoid huge payloads (no full logs)
    query = """
        SELECT
            j.id, j.archive_id, j.job_type, j.status,
            j.start_time, j.end_time, j.total_size_bytes, j.reclaimed_size_bytes,
            j.is_dry_run, j.triggered_by,
            a.name as archive_name,
            CASE WHEN j.status = 'running' THEN NULL ELSE EXTRACT(EPOCH FROM (j.end_time - j.start_time))::INTEGER END as duration_seconds,
            (SELECT STRING_AGG(stack_name, ',') FROM job_stack_metrics WHERE job_id = j.id) as stack_names
        FROM jobs j
        LEFT JOIN archives a ON j.archive_id = a.id
        WHERE 1=1
    """
    params = []
    
    if archive_id:
        query += " AND j.archive_id = %s"
        params.append(archive_id)
    
    if job_type:
        query += " AND j.job_type = %s"
        params.append(job_type)
    
    query += f" ORDER BY j.start_time DESC LIMIT {limit};"
    
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        jobs = cur.fetchall()
    
    # Convert any datetime fields to ISO UTC strings with 'Z' so the client parses them
    jobs_out = []
    for j in jobs:
        jd = dict(j)
        for k in ('start_time', 'end_time'):
            if jd.get(k):
                jd[k] = utils.to_iso_z(jd[k])
        jobs_out.append(jd)

    return jsonify({
        'jobs': jobs_out
    })


def _get_base_url():
    """Get base URL from settings."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = 'base_url';")
        result = cur.fetchone()
        return result['value'] if result else 'http://localhost:8080'


@bp.route('/cleanup/run', methods=['POST'])
@api_auth_required
def run_cleanup_manual():
    """Trigger a manual cleanup run. Accepts JSON: {"dry_run": true|false}.

    The cleanup is started in a background thread and returns 202 Accepted.
    """
    try:
        data = request.get_json() or {}
        dry_run = data.get('dry_run', None)
        try:
            # Create a job record immediately and return its id so the UI can link to details
            with get_db() as conn:
                cur = conn.cursor()
                start_time = __import__('app').app.utils.now()
                cur.execute("""
                    INSERT INTO jobs (job_type, status, start_time, triggered_by, is_dry_run, log)
                    VALUES ('cleanup', 'running', %s, 'manual', %s, '')
                    RETURNING id;
                """, (start_time, dry_run))
                job_id = cur.fetchone()['id']
                conn.commit()

            from app import cleanup as _cleanup
            import threading
            # Start background task and pass the job_id so the task updates the same job record
            t = threading.Thread(target=_cleanup.run_cleanup, args=(dry_run, job_id), daemon=True)
            t.start()
            return jsonify({'success': True, 'message': 'Cleanup started', 'job_id': job_id}), 202
        except Exception as e:
            return jsonify({'error': f'Failed to start cleanup: {e}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/downloads/status')
@api_auth_required
def download_status():
    """Return status for a given download token (used by client polling)."""
    token = request.args.get('token')
    if not token:
        return jsonify({'error': 'token required'}), 400

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT token, archive_path, is_preparing, is_folder, expires_at FROM download_tokens WHERE token = %s;", (token,))
        row = cur.fetchone()

    if not row:
        return jsonify({'error': 'token not found'}), 404

    archive_path = row.get('archive_path')
    is_preparing = bool(row.get('is_preparing'))
    ready = True if (archive_path and os.path.exists(archive_path)) else False

    download_url = None
    if ready:
        base_url = _get_base_url().rstrip('/')
        download_url = f"{base_url}/download/{token}"

    return jsonify({
        'token': token,
        'is_preparing': is_preparing,
        'ready': ready,
        'download_url': download_url
    })

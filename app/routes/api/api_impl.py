"""
API routes for JSON endpoints.
Supports both session-based auth (for web UI) and API token auth (for external calls).
"""
import secrets
import os
import subprocess
import threading
import json
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


# Job-related endpoints moved to `jobs.py` to keep API package modular.
# See `app.routes.api.jobs` for implementations.

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
            # Send an initial JSON 'connected' event so the client receives an onmessage and
            # clears its idle watchdog (previously only a comment was sent, which doesn't
            # trigger onmessage).
            yield 'data: ' + json.dumps({'type': 'connected', 'data': {}}) + '\n\n'
            while True:
                try:
                    msg = q.get(timeout=15)
                except Exception:
                    # keepalive comment to keep connection alive
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
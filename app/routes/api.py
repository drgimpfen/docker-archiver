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
from flask import Blueprint, request, jsonify, send_file
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
    
    return jsonify({
        'job': dict(job),
        'metrics': [dict(m) for m in metrics]
    })


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
        cur.execute("SELECT j.log, a.name as archive_name FROM jobs j LEFT JOIN archives a ON j.archive_id = a.id WHERE j.id = %s;", (job_id,))
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





def _prepare_folder_download(token, folder_path, stack_name, user_email):
    """Background task to compress folder and notify user."""
    try:
        # Create compressed archive
        download_dir = Path('/archives/_downloads')
        download_dir.mkdir(exist_ok=True)
        
        timestamp = utils.local_now().strftime('%Y%m%d_%H%M%S')
        archive_name = f"{stack_name}_{timestamp}.tar.zst"
        archive_path = download_dir / archive_name
        
        # Compress folder
        subprocess.run(
            ['tar', '-I', 'zstd', '-cf', str(archive_path), '-C', str(Path(folder_path).parent), Path(folder_path).name],
            check=True,
            timeout=3600
        )
        
        # Update token with new path
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE download_tokens 
                SET archive_path = %s, is_folder = false 
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
                
                base_url = _get_base_url()
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
        
        # Start archive job in background
        from app.executor import ArchiveExecutor
        
        def run_job():
            executor = ArchiveExecutor(dict(archive), is_dry_run=False)
            executor.run(triggered_by='api')
        
        threading.Thread(target=run_job).start()
        
        return jsonify({
            'success': True,
            'message': f"Archive '{archive['name']}' started",
            'archive_id': archive_id
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
        
        # Start dry run in background
        from app.executor import ArchiveExecutor
        
        def run_job():
            executor = ArchiveExecutor(dict(archive), is_dry_run=True, dry_run_config=dry_run_config)
            executor.run(triggered_by='api_dry_run')
        
        threading.Thread(target=run_job).start()
        
        return jsonify({
            'success': True,
            'message': f"Dry run for '{archive['name']}' started",
            'archive_id': archive_id,
            'dry_run_config': dry_run_config
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
    limit = request.args.get('limit', type=int, default=100)
    
    query = """
        SELECT j.*, a.name as archive_name,
               (SELECT STRING_AGG(stack_name, ',') 
                FROM job_stack_metrics 
                WHERE job_id = j.id) as stack_names
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
    
    return jsonify({
        'jobs': [dict(j) for j in jobs]
    })


def _get_base_url():
    """Get base URL from settings."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = 'base_url';")
        result = cur.fetchone()
        return result['value'] if result else 'http://localhost:8080'

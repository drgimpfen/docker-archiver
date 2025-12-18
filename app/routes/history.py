"""
Job history routes.
"""
import secrets
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, send_file
from app.auth import login_required, get_current_user
from app.db import get_db
from app.utils import format_bytes, format_duration
from app.notifications import send_test_notification


bp = Blueprint('history', __name__, url_prefix='/history')


@bp.route('/')
@login_required
def list_history():
    """Job history page."""
    # Get filter parameters
    archive_id = request.args.get('archive_id', type=int)
    job_type = request.args.get('type')
    
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
    
    query += " ORDER BY j.start_time DESC LIMIT 100;"
    
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        jobs = cur.fetchall()
        
        # Get all archives for filter dropdown
        cur.execute("SELECT id, name FROM archives ORDER BY name;")
        archive_list = cur.fetchall()
    
    return render_template(
        'history.html',
        jobs=jobs,
        archives=archive_list,
        format_bytes=format_bytes,
        format_duration=format_duration,
        current_user=get_current_user()
    )


@bp.route('/api/job/<int:job_id>')
@login_required
def get_job_details(job_id):
    """Get job details (for modal)."""
    with get_db() as conn:
        cur = conn.cursor()
        
        # Get job
        cur.execute("""
            SELECT j.*, a.name as archive_name 
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
    
    return jsonify({
        'job': dict(job),
        'metrics': [dict(m) for m in metrics]
    })


@bp.route('/api/job/<int:job_id>/request-download', methods=['POST'])
@login_required
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
        expires_at = datetime.now() + timedelta(hours=24)
        
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
                'expires_in': '24 hours',
                'is_folder': False
            })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/download/<token>')
def download_archive(token):
    """Download archive using token (no authentication required)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM download_tokens 
            WHERE token = %s AND expires_at > NOW();
        """, (token,))
        token_data = cur.fetchone()
    
    if not token_data:
        return "Download link expired or invalid", 404
    
    archive_path = token_data['archive_path']
    
    # Check if archive exists
    if not os.path.exists(archive_path):
        return "Archive file not found", 404
    
    # Send file
    filename = Path(archive_path).name
    return send_file(archive_path, as_attachment=True, download_name=filename)


@bp.route('/api/job/<int:job_id>/download-log')
@login_required
def download_log(job_id):
    """Download job log as text file."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT log, archive_name FROM jobs j LEFT JOIN archives a ON j.archive_id = a.id WHERE j.id = %s;", (job_id,))
        result = cur.fetchone()
    
    if not result or not result['log']:
        return "No log available", 404
    
    # Create temporary log file
    log_filename = f"job_{job_id}_{result['archive_name'] or 'unknown'}.log"
    log_path = f"/tmp/{log_filename}"
    
    with open(log_path, 'w') as f:
        f.write(result['log'])
    
    return send_file(log_path, as_attachment=True, download_name=log_filename)


def _prepare_folder_download(token, folder_path, stack_name, user_email):
    """Background task to compress folder and notify user."""
    try:
        # Create compressed archive
        download_dir = Path('/archives/_downloads')
        download_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
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


def _get_base_url():
    """Get base URL from settings."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = 'base_url';")
        result = cur.fetchone()
        return result['value'] if result else 'http://localhost:8080'

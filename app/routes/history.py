"""
Job history routes.
"""
from flask import Blueprint, render_template, request
from app.auth import login_required, get_current_user
from app.db import get_db
from app.utils import format_bytes, format_duration


bp = Blueprint('history', __name__, url_prefix='/history')


@bp.route('/')
@login_required
def list_history():
    """Job history page."""
    # Get filter parameters
    archive_id = request.args.get('archive_id', type=int)
    job_type = request.args.get('type')
    
    query = """
        SELECT j.*, a.name as archive_name, a.stacks as archive_stacks,
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
    
    query += " ORDER BY j.start_time DESC, j.id DESC LIMIT 100;"
    
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

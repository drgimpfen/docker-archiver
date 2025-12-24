from flask import request, jsonify, send_file
from app.routes.api import bp, api_auth_required
from app.db import get_db
from app import utils


@bp.route('/jobs/<int:job_id>')
@bp.route('/jobs/<int:job_id>/')
@api_auth_required
def get_job_details(job_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT j.*, a.name as archive_name, a.output_format as archive_output_format,
                   EXTRACT(EPOCH FROM (j.end_time - j.start_time))::INTEGER as duration_seconds
            FROM jobs j
            LEFT JOIN archives a ON j.archive_id = a.id
            WHERE j.id = %s;
        """, (job_id,))
        job = cur.fetchone()
        if not job:
            return jsonify({'error': 'Job not found'}), 404
        cur.execute("""
            SELECT * FROM job_stack_metrics WHERE job_id = %s ORDER BY start_time;
        """, (job_id,))
        metrics = cur.fetchall()
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
            if hasattr(val, 'astimezone'):
                md[key] = utils.to_iso_z(val)
        metrics_out.append(md)

    return jsonify({'job': job_out, 'metrics': metrics_out})


@bp.route('/jobs/<int:job_id>/log')
@api_auth_required
def download_log(job_id):
    stack_name = request.args.get('stack')
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT j.log, a.name as archive_name, j.status FROM jobs j LEFT JOIN archives a ON j.archive_id = a.id WHERE j.id = %s;", (job_id,))
        result = cur.fetchone()
    if not result or not result['log']:
        return "No log available", 404
    log_content = result['log']
    if stack_name:
        lines = log_content.split('\n')
        filtered = []
        in_stack = False
        for line in lines:
            if f"--- Starting backup for stack: {stack_name} ---" in line:
                in_stack = True
            elif f"--- Finished backup for stack: {stack_name} ---" in line:
                filtered.append(line)
                in_stack = False
            elif "--- Starting backup for stack:" in line and f"stack: {stack_name} ---" not in line:
                in_stack = False
            if in_stack:
                filtered.append(line)
        log_content = '\n'.join(filtered)
        if not log_content.strip():
            return f"No log entries found for stack '{stack_name}'", 404
    filename_suffix = f"_{stack_name}" if stack_name else ""
    log_filename = f"job_{job_id}_{result['archive_name'] or 'unknown'}{filename_suffix}.log"
    log_path = f"/tmp/{log_filename}"
    with open(log_path, 'w') as f:
        f.write(log_content)
    return send_file(log_path, as_attachment=True, download_name=log_filename)


@bp.route('/jobs', methods=['GET'])
@api_auth_required
def list_jobs():
    """List jobs with optional filters (API endpoint)."""
    archive_id = request.args.get('archive_id', type=int)
    job_type = request.args.get('type')
    limit = request.args.get('limit', type=int, default=20)

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

    jobs_out = []
    for j in jobs:
        jd = dict(j)
        for k in ('start_time', 'end_time'):
            if jd.get(k):
                jd[k] = utils.to_iso_z(jd[k])
        jobs_out.append(jd)

    return jsonify({'jobs': jobs_out})


@bp.route('/jobs/<int:job_id>/log/tail')
@api_auth_required
def tail_log(job_id):
    last_line = request.args.get('last_line', type=int, default=0)
    stack_name = request.args.get('stack')
    try:
        from app.executor import get_running_executor
        executor = get_running_executor(job_id)
    except Exception:
        executor = None
    lines = []
    complete = True
    if executor:
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
        if len(db_lines) >= len(mem_lines):
            lines = db_lines
        else:
            if db_lines and mem_lines[:len(db_lines)] == db_lines:
                lines = db_lines + mem_lines[len(db_lines):]
            else:
                merged = db_lines.copy()
                for l in mem_lines:
                    if not merged or merged[-1] != l:
                        merged.append(l)
                lines = merged
        complete = False
    else:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT j.log, j.status FROM jobs j WHERE j.id = %s;", (job_id,))
            result = cur.fetchone()
            if not result or not result['log']:
                return jsonify({'lines': [], 'last_line': 0, 'complete': True})
            lines = result['log'].split('\n')
            complete = result.get('status') != 'running'
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
    if last_line < 0:
        last_line = 0
    if last_line > total_lines:
        last_line = total_lines
    new_lines = lines[last_line:]
    new_last_line = total_lines
    job_meta = {}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, status, start_time, end_time, duration_seconds, total_size_bytes, reclaimed_bytes FROM jobs WHERE id = %s;", (job_id,))
        row = cur.fetchone()
        if row:
            job_meta = dict(row)
    return jsonify({'lines': new_lines, 'last_line': new_last_line, 'complete': complete, 'job': job_meta})



def _get_base_url():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = 'base_url';")
        result = cur.fetchone()
        return result['value'] if result else 'http://localhost:8080'
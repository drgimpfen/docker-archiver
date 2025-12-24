"""Cleanup-related API endpoints."""
from datetime import datetime
from flask import request, jsonify
from app.routes.api import bp, api_auth_required
from app.db import get_db
from app import utils
import threading


@bp.route('/cleanup/run', methods=['POST'])
@api_auth_required
def run_cleanup_api():
    """Trigger a manual cleanup run (supports dry run). Returns 202 with job_id if started."""
    try:
        data = request.get_json() or {}
        dry = bool(data.get('dry_run', False))
        # Create a job record so the UI can link to it immediately
        job_id = None
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO jobs (job_type, status, start_time, triggered_by, is_dry_run, log)
                    VALUES ('cleanup', 'running', %s, 'manual', %s, '')
                    RETURNING id;
                """, (datetime.utcnow(), dry))
                job_id = cur.fetchone()['id']
                conn.commit()
        except Exception as e:
            # Non-fatal: log and continue without job_id
            utils.get_logger(__name__).exception("Failed to create cleanup job record: %s", e)

        def _runner():
            try:
                # Import here to avoid circular imports at module import time
                from app.cleanup import run_cleanup
                run_cleanup(dry_run_override=dry, job_id=job_id)
            except Exception as e:
                # Ensure the job is marked as failed if something goes wrong
                try:
                    with get_db() as conn:
                        cur = conn.cursor()
                        end_time = datetime.utcnow()
                        cur.execute("""
                            UPDATE jobs SET status = 'failed', end_time = %s, error_message = %s WHERE id = %s;
                        """, (end_time, str(e), job_id))
                        conn.commit()
                except Exception:
                    pass
        # Run in background thread
        threading.Thread(target=_runner, daemon=True).start()
        return jsonify({'status': 'started', 'job_id': job_id}), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500
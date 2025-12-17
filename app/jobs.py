import traceback
from datetime import datetime
from psycopg2.extras import DictCursor
from db import get_db_connection


def _safe_execute(sql, params=None, fetch=False):
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(sql, params or ())
            if fetch:
                res = cur.fetchone()
            else:
                res = None
            conn.commit()
        try:
            conn.close()
        except Exception:
            pass
        return res
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None


def create_job_for_archive_master(legacy_archive_id, job_type, start_time, status, description, log):
    sql = """
    INSERT INTO jobs (legacy_archive_id, job_type, start_time, status, description, log)
    VALUES (%s,%s,%s,%s,%s,%s) RETURNING id;
    """
    res = _safe_execute(sql, (legacy_archive_id, job_type, start_time, status, description, log), fetch=True)
    return res['id'] if res else None


def create_job_for_archive_stack(parent_id, legacy_archive_id, job_type, stack_name, start_time, status, log):
    sql = """
    INSERT INTO jobs (parent_id, legacy_archive_id, job_type, stack_name, start_time, status, log)
    VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id;
    """
    res = _safe_execute(sql, (parent_id, legacy_archive_id, job_type, stack_name, start_time, status, log), fetch=True)
    return res['id'] if res else None


def create_job_for_retention(legacy_retention_id, parent_id, job_type, start_time, status, description, log):
    sql = """
    INSERT INTO jobs (legacy_retention_id, parent_id, job_type, start_time, status, description, log)
    VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id;
    """
    res = _safe_execute(sql, (legacy_retention_id, parent_id, job_type, start_time, status, description, log), fetch=True)
    return res['id'] if res else None


def update_job_by_legacy_archive(legacy_archive_id, **fields):
    if not fields:
        return False
    parts = []
    params = []
    # treat 'log' specially: append
    for k, v in fields.items():
        if k == 'log':
            parts.append("log = COALESCE(log,'') || %s")
            params.append(v)
        else:
            parts.append(f"{k} = %s")
            params.append(v)
    params.append(legacy_archive_id)
    sql = f"UPDATE jobs SET {', '.join(parts)} WHERE legacy_archive_id = %s;"
    _safe_execute(sql, tuple(params))
    return True


def update_job_by_legacy_retention(legacy_retention_id, **fields):
    if not fields:
        return False
    parts = []
    params = []
    for k, v in fields.items():
        if k == 'log':
            parts.append("log = COALESCE(log,'') || %s")
            params.append(v)
        else:
            parts.append(f"{k} = %s")
            params.append(v)
    params.append(legacy_retention_id)
    sql = f"UPDATE jobs SET {', '.join(parts)} WHERE legacy_retention_id = %s;"
    _safe_execute(sql, tuple(params))
    return True


def append_log_by_legacy_archive(legacy_archive_id, message):
    if not message:
        return False
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return update_job_by_legacy_archive(legacy_archive_id, log=f"[{ts}] {message}\n")


def append_log_by_legacy_retention(legacy_retention_id, message):
    if not message:
        return False
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return update_job_by_legacy_retention(legacy_retention_id, log=f"[{ts}] {message}\n")

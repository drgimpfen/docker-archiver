"""
Database schema and connection management for Docker Archiver.
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from app.utils import setup_logging, get_logger
from app import utils

# Configure logging using centralized setup so LOG_LEVEL is respected
setup_logging()
logger = get_logger(__name__)


def get_db_url():
    """Get database URL from environment."""
    return os.environ.get('DATABASE_URL', 'postgresql://archiver:changeme123@localhost:5432/docker_archiver')


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = psycopg2.connect(get_db_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Initialize database schema."""
    with get_db() as conn:
        cur = conn.cursor()
        
        # Users table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                email VARCHAR(255),
                role VARCHAR(50) DEFAULT 'admin',
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMPTZ
            );
        """)
        
        # Archives configuration table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS archives (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) UNIQUE NOT NULL,
                stacks TEXT[] NOT NULL,
                stop_containers BOOLEAN DEFAULT true,
                schedule_enabled BOOLEAN DEFAULT false,
                schedule_cron VARCHAR(100),
                output_format VARCHAR(20) DEFAULT 'tar',
                retention_keep_days INTEGER DEFAULT 7,
                retention_keep_weeks INTEGER DEFAULT 4,
                retention_keep_months INTEGER DEFAULT 6,
                retention_keep_years INTEGER DEFAULT 2,
                retention_one_per_day BOOLEAN DEFAULT false,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Jobs table (archive and retention runs)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id SERIAL PRIMARY KEY,
                archive_id INTEGER REFERENCES archives(id) ON DELETE CASCADE,
                job_type VARCHAR(50) NOT NULL,
                status VARCHAR(50) DEFAULT 'running',
                start_time TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMPTZ,
                duration_seconds INTEGER,
                total_size_bytes BIGINT DEFAULT 0,
                reclaimed_bytes BIGINT DEFAULT 0,
                is_dry_run BOOLEAN DEFAULT false,
                dry_run_config JSONB,
                log TEXT,
                error TEXT,
                triggered_by VARCHAR(50) DEFAULT 'manual'
            );
        """)
        
        # Job stack metrics (per-stack details within a job)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS job_stack_metrics (
                id SERIAL PRIMARY KEY,
                job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
                stack_name VARCHAR(255) NOT NULL,
                status VARCHAR(50) DEFAULT 'pending',
                start_time TIMESTAMPTZ,
                end_time TIMESTAMPTZ,
                duration_seconds INTEGER,
                archive_path TEXT,
                archive_size_bytes BIGINT DEFAULT 0,
                was_running BOOLEAN,
                log TEXT,
                error TEXT
            );
        """)
        
        # Download tokens for secure file access
        cur.execute("""
            CREATE TABLE IF NOT EXISTS download_tokens (
                id SERIAL PRIMARY KEY,
                token VARCHAR(64) UNIQUE NOT NULL,
                stack_name VARCHAR(255) NOT NULL,
                file_path TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMPTZ NOT NULL,
                is_packing BOOLEAN DEFAULT FALSE
            );
        """)
        
        # Migrate download_tokens table: if older schema exists without new columns, add them
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='download_tokens' AND column_name='file_path'
                ) THEN
                    ALTER TABLE download_tokens ADD COLUMN file_path TEXT;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='download_tokens' AND column_name='created_at'
                ) THEN
                    ALTER TABLE download_tokens ADD COLUMN created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='download_tokens' AND column_name='expires_at'
                ) THEN
                    ALTER TABLE download_tokens ADD COLUMN expires_at TIMESTAMPTZ;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='download_tokens' AND column_name='is_packing'
                ) THEN
                    ALTER TABLE download_tokens ADD COLUMN is_packing BOOLEAN DEFAULT FALSE;
                END IF;
            END $$;
        """)

        # Ensure `notify_emails` array column exists and drop legacy `notify_email` column if present
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='download_tokens' AND column_name='notify_emails'
                ) THEN
                    ALTER TABLE download_tokens ADD COLUMN notify_emails TEXT[];
                END IF;

                -- If legacy notify_email column exists, drop it (we assume backfill has already been applied)
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='download_tokens' AND column_name='notify_email'
                ) THEN
                    ALTER TABLE download_tokens DROP COLUMN notify_email;
                END IF;
            END $$;
        """)

        # Ensure archive_path column exists and is nullable; backfill from file_path when possible
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='download_tokens' AND column_name='archive_path'
                ) THEN
                    ALTER TABLE download_tokens ADD COLUMN archive_path TEXT;
                END IF;

                -- If archive_path exists but is NOT NULL, drop the NOT NULL constraint so INSERTs without it succeed
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='download_tokens' AND column_name='archive_path' AND is_nullable = 'NO'
                ) THEN
                    ALTER TABLE download_tokens ALTER COLUMN archive_path DROP NOT NULL;
                END IF;

                -- Backfill archive_path from file_path for existing rows
                IF EXISTS (SELECT 1 FROM download_tokens WHERE archive_path IS NULL AND file_path IS NOT NULL) THEN
                    UPDATE download_tokens SET archive_path = file_path WHERE archive_path IS NULL AND file_path IS NOT NULL;
                END IF;
            END $$;
        """)
        
        # API tokens for external access
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_tokens (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                token VARCHAR(64) UNIQUE NOT NULL,
                name VARCHAR(255),
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMPTZ,
                last_used_at TIMESTAMPTZ
            );
        """)
        
        # Settings table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(255) PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Create indices
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_archive_id ON jobs(archive_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_start_time ON jobs(start_time DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_job_stack_metrics_job_id ON job_stack_metrics(job_id);")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_download_tokens_token ON download_tokens(token);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_download_tokens_expires_at ON download_tokens(expires_at);")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_token ON api_tokens(token);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_user_id ON api_tokens(user_id);")
        

        
        # Migrate jobs table - add missing columns
        cur.execute("""
            DO $$ 
            BEGIN
                -- Add reclaimed_size_bytes as alias for reclaimed_bytes
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='jobs' AND column_name='reclaimed_size_bytes'
                ) THEN
                    ALTER TABLE jobs ADD COLUMN reclaimed_size_bytes BIGINT DEFAULT 0;
                END IF;
                
                -- Add error_message as alias for error
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='jobs' AND column_name='error_message'
                ) THEN
                    ALTER TABLE jobs ADD COLUMN error_message TEXT;
                END IF;

                -- Add deleted counts for retention reporting
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='jobs' AND column_name='deleted_count'
                ) THEN
                    ALTER TABLE jobs ADD COLUMN deleted_count INTEGER DEFAULT 0;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='jobs' AND column_name='deleted_dirs'
                ) THEN
                    ALTER TABLE jobs ADD COLUMN deleted_dirs INTEGER DEFAULT 0;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='jobs' AND column_name='deleted_files'
                ) THEN
                    ALTER TABLE jobs ADD COLUMN deleted_files INTEGER DEFAULT 0;
                END IF;
            END $$;
        """)
        
        # Migrate job_stack_metrics - add deleted_at column
        cur.execute("""
            DO $$ 
            BEGIN
                -- Add deleted_at timestamp for retention tracking
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='job_stack_metrics' AND column_name='deleted_at'
                ) THEN
                    ALTER TABLE job_stack_metrics ADD COLUMN deleted_at TIMESTAMPTZ;
                END IF;
                
                -- Add deleted_by column to track what deleted it
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='job_stack_metrics' AND column_name='deleted_by'
                ) THEN
                    ALTER TABLE job_stack_metrics ADD COLUMN deleted_by VARCHAR(50);
                END IF;
            END $$;
        """)
        



        # Insert default settings if not exist
        cur.execute("""
            INSERT INTO settings (key, value) VALUES 
                ('base_url', 'http://localhost:8080'),
                ('notify_on_success', 'true'),
                ('notify_on_error', 'true'),
                ('maintenance_mode', 'false'),
                ('cleanup_enabled', 'true'),
                ('cleanup_cron', '30 2 * * *'),
                ('cleanup_log_retention_days', '90'),
                ('cleanup_dry_run', 'false'),
                ('notify_on_cleanup', 'false'),
                ('notify_attach_log', 'false'),
                ('notify_attach_log_on_failure', 'true'),
                ('smtp_server', ''),
                ('smtp_port', '587'),
                ('smtp_user', ''),
                ('smtp_password', ''),
                ('smtp_from', ''),
                ('smtp_use_tls', 'true'),
                ('apply_permissions', 'false'),
                ('image_pull_policy', 'pull-on-miss'),
                ('image_pull_inactivity_timeout', '300'),
                ('image_pull_excerpt_lines', '8'),
                ('app_version', '0.8.2')
            ON CONFLICT (key) DO NOTHING;
        """)
        
        # Update app version if it changed (avoid importing main.py during init)
        try:
            from app.main import __version__
            cur.execute("""
                INSERT INTO settings (key, value) VALUES ('app_version', %s)
                ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP;
            """, (__version__, __version__))
        except Exception:
            # During initial setup, main.py might not be importable yet
            pass
        
        conn.commit()
        logger.info("[DB] Database schema initialized successfully")

        # Ensure timestamp columns are timezone-aware (timestamptz) and migrate if needed
        try:
            migrate_timestamp_columns()
        except Exception as e:
            logger.exception("[DB] Timestamp migration failed: %s", e)


def migrate_timestamp_columns():
    """Detect TIMESTAMP columns and migrate them to TIMESTAMPTZ interpreting
    existing naive timestamps as UTC. This is performed automatically at
    application startup to ensure consistent timezone-aware storage.
    """
    cols = [
        ('jobs', 'start_time'),
        ('jobs', 'end_time'),
        ('job_stack_metrics', 'start_time'),
        ('job_stack_metrics', 'end_time'),
        ('job_stack_metrics', 'deleted_at'),
        ('download_tokens', 'created_at'),
        ('download_tokens', 'expires_at'),
        ('download_tokens', 'last_used_at'),
        ('users', 'created_at'),
        ('users', 'updated_at'),
        ('users', 'last_login'),
        ('archives', 'created_at'),
        ('archives', 'updated_at'),
    ]

    with get_db() as conn:
        cur = conn.cursor()
        for table, col in cols:
            try:
                cur.execute("SELECT data_type FROM information_schema.columns WHERE table_name = %s AND column_name = %s", (table, col))
                r = cur.fetchone()
                if not r:
                    continue
                dt = r.get('data_type') if isinstance(r, dict) else r[0]
                if dt == 'timestamp without time zone':
                    logger.info('[DB] Migrating %s.%s from TIMESTAMP to TIMESTAMPTZ', table, col)
                    try:
                        cur.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE timestamptz USING {col} AT TIME ZONE 'UTC';")
                        conn.commit()
                        logger.info('[DB] Migrated %s.%s successfully', table, col)
                    except Exception:
                        logger.exception('[DB] Failed to migrate %s.%s', table, col)
                        conn.rollback()
            except Exception:
                logger.exception('[DB] Failed to check column %s.%s', table, col)


def is_archive_running(archive_id):
    """Return True if there is a running job for the given archive id."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE archive_id = %s AND status = 'running';", (archive_id,))
        row = cur.fetchone()
        return bool(row and row.get('cnt', 0) > 0)


def mark_stale_running_jobs(threshold_minutes=None):
    """Mark running jobs as failed at startup.

    If ``threshold_minutes`` is ``None`` (default) this function will mark *all* jobs with
    ``status = 'running'`` and ``end_time IS NULL`` as ``failed`` (sets ``end_time`` and
    ``duration_seconds`` where available). If ``threshold_minutes`` is provided, it will only
    mark jobs whose ``start_time`` is older than the threshold (legacy behavior).

    Returns the number of jobs that were marked as failed.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            msg = 'Marked failed on server startup (stale running job)'
            log_line = f"[{utils.local_now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Job marked failed due to server restart\n"

            if threshold_minutes is None:
                # Mark any running jobs missing an end_time as failed
                now_ts = utils.now()
                cur.execute("""
                    UPDATE jobs
                    SET status = 'failed',
                        end_time = %s,
                        duration_seconds = CASE WHEN start_time IS NOT NULL
                            THEN EXTRACT(EPOCH FROM (%s - start_time))::INTEGER
                            ELSE NULL END,
                        error = COALESCE(error, '') || %s,
                        log = COALESCE(log, '') || %s
                    WHERE status = 'running' AND end_time IS NULL;
                """, (now_ts, now_ts, msg, log_line))
                count = cur.rowcount
                conn.commit()
                return count
            else:
                # Legacy: mark only jobs older than threshold
                now_ts = utils.now()
                cur.execute("""
                    UPDATE jobs
                    SET status = 'failed',
                        end_time = %s,
                        duration_seconds = EXTRACT(EPOCH FROM (%s - start_time))::INTEGER,
                        error = COALESCE(error, '') || %s,
                        log = COALESCE(log, '') || %s
                    WHERE status = 'running'
                      AND start_time < (%s - (%s || ' minutes')::interval);
                """, (now_ts, now_ts, msg, log_line, now_ts, str(threshold_minutes)))
                count = cur.rowcount
                conn.commit()
                return count
    except Exception as e:
        logger.exception("[DB] Failed to mark stale running jobs: %s", e)
        return 0


if __name__ == '__main__':
    init_db()

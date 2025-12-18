"""
Database schema and connection management for Docker Archiver.
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager


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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Jobs table (archive and retention runs)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id SERIAL PRIMARY KEY,
                archive_id INTEGER REFERENCES archives(id) ON DELETE CASCADE,
                job_type VARCHAR(50) NOT NULL,
                status VARCHAR(50) DEFAULT 'running',
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMP,
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
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                duration_seconds INTEGER,
                archive_path TEXT,
                archive_size_bytes BIGINT DEFAULT 0,
                was_running BOOLEAN,
                log TEXT,
                error TEXT
            );
        """)
        
        # Download tokens
        cur.execute("""
            CREATE TABLE IF NOT EXISTS download_tokens (
                id SERIAL PRIMARY KEY,
                token VARCHAR(64) UNIQUE NOT NULL,
                job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
                stack_name VARCHAR(255),
                file_path TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                downloads INTEGER DEFAULT 0
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_download_tokens_expires ON download_tokens(expires_at);")
        
        # Insert default settings if not exist
        cur.execute("""
            INSERT INTO settings (key, value) VALUES 
                ('base_url', 'http://localhost:8080'),
                ('apprise_urls', ''),
                ('notify_on_success', 'true'),
                ('notify_on_error', 'true'),
                ('maintenance_mode', 'false')
            ON CONFLICT (key) DO NOTHING;
        """)
        
        conn.commit()
        print("[DB] Database schema initialized successfully")


if __name__ == '__main__':
    init_db()

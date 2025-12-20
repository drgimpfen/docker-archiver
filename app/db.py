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
                archive_path TEXT NOT NULL,
                is_folder BOOLEAN DEFAULT false,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                downloads INTEGER DEFAULT 0
            );
        """)
        
        # API tokens for external access
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_tokens (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                token VARCHAR(64) UNIQUE NOT NULL,
                name VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                last_used_at TIMESTAMP
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_token ON api_tokens(token);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_user_id ON api_tokens(user_id);")
        
        # Migrate download_tokens table if needed (rename file_path to archive_path, add is_folder)
        cur.execute("""
            DO $$ 
            BEGIN
                -- Check if old column exists and rename it
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='download_tokens' AND column_name='file_path'
                ) THEN
                    ALTER TABLE download_tokens RENAME COLUMN file_path TO archive_path;
                END IF;
                
                -- Add is_folder column if it doesn't exist
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='download_tokens' AND column_name='is_folder'
                ) THEN
                    ALTER TABLE download_tokens ADD COLUMN is_folder BOOLEAN DEFAULT false;
                END IF;
            END $$;
        """)
        
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
                    ALTER TABLE job_stack_metrics ADD COLUMN deleted_at TIMESTAMP;
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
                ('apprise_urls', ''),
                ('notify_on_success', 'true'),
                ('notify_on_error', 'true'),
                ('maintenance_mode', 'false'),
                ('max_token_downloads', '3'),
                ('cleanup_enabled', 'true'),
                ('cleanup_time', '02:30'),
                ('cleanup_log_retention_days', '90'),
                ('cleanup_dry_run', 'false'),
                ('notify_on_cleanup', 'false'),
                ('notify_report_verbosity', 'full'),
                ('notify_attach_log', 'false'),
                ('notify_attach_log_on_failure', 'false'),
                ('app_version', '0.7.0')
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
        print("[DB] Database schema initialized successfully")


if __name__ == '__main__':
    init_db()

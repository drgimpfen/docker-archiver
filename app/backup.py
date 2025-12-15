import subprocess
import os
import sys
from datetime import datetime
import psycopg2
from psycopg2.extras import DictCursor

# --- HELPER FUNCTIONS ---

def get_db_connection():
    """Establishes a connection to the database using environment variables."""
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(database_url)

def format_bytes(size_bytes):
    """Converts bytes to human-readable format."""
    if size_bytes is None or size_bytes < 0:
        return "N/A"
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "K", "M", "G", "T")
    i = 0
    while size_bytes >= 1024 and i < len(size_name) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.1f}{size_name[i]}"

def log_to_db(job_id, message):
    """Appends a log message to a specific job entry in the database."""
    print(f"[Job {job_id}] {message}") # Also print to container logs
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE archive_jobs SET log = log || %s WHERE id = %s;",
            (f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n", job_id)
        )
        conn.commit()
    conn.close()

def run_command(command, job_id, description):
    """Executes a shell command and logs the result to the DB."""
    try:
        log_to_db(job_id, f"Running: {description}...")
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=300)
        log_to_db(job_id, f"Success: {description}.")
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        error_message = f"Failed: {description}. Error:\n{e.stderr.strip()}"
        log_to_db(job_id, error_message)
        raise RuntimeError(error_message) # Propagate failure
    except subprocess.TimeoutExpired:
        error_message = f"Timeout: {description} took too long to execute."
        log_to_db(job_id, error_message)
        raise RuntimeError(error_message)
    except FileNotFoundError:
        error_message = "Error: A command (like 'docker' or 'tar') was not found."
        log_to_db(job_id, error_message)
        raise RuntimeError(error_message)

def compose_action(stack_path, job_id, action="down"):
    """Stops ('down') or starts ('up' -d) a Docker Compose stack."""
    if not os.path.isdir(stack_path):
        log_to_db(job_id, f"Stack directory not found at {stack_path}. Skipping {action}.")
        return
    
    action_text = "Stopping" if action == "down" else "Starting"
    compose_file = "compose.yaml" if os.path.exists(os.path.join(stack_path, "compose.yaml")) else "docker-compose.yml"
    cmd = ["docker", "compose", "-f", os.path.join(stack_path, compose_file), action]
    if action == "up":
        cmd.append("-d")
        
    run_command(cmd, job_id, f"{action_text} stack in {stack_path}")

def create_archive(stack_name, stack_path, backup_dir, job_id):
    """Creates a TAR archive and returns its path and size."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name_base = f"{stack_name}_{timestamp}.tar"
    final_backup_path = os.path.join(backup_dir, stack_name)
    os.makedirs(final_backup_path, exist_ok=True)
    
    target_filename = os.path.join(final_backup_path, archive_name_base)
    
    archive_root_dir = os.path.dirname(stack_path)
    dir_to_archive = os.path.basename(stack_path)

    cmd = ["tar", "-c", "-f", target_filename, "-C", archive_root_dir, dir_to_archive]
    
    run_command(cmd, job_id, f"Archiving {stack_name}")
    
    size_bytes = os.path.getsize(target_filename)
    log_to_db(job_id, f"Archive created: {target_filename}. Size: {format_bytes(size_bytes)}.")
    return target_filename, size_bytes

def cleanup_local_archives(archive_dir, retention_days_str, master_job_id):
    """Deletes old local archives based on retention days."""
    try:
        retention_days = int(retention_days_str)
    except (ValueError, TypeError):
        log_to_db(master_job_id, f"Invalid retention period '{retention_days_str}'. Skipping cleanup.")
        return

    log_to_db(master_job_id, f"Starting cleanup. Deleting archives older than {retention_days} days in {archive_dir}.")
    
    deleted_count = 0
    freed_space = 0

    for root, dirs, files in os.walk(archive_dir):
        for file in files:
            if file.endswith(".tar"):
                file_path = os.path.join(root, file)
                file_mtime = os.path.getmtime(file_path)
                if (datetime.now() - datetime.fromtimestamp(file_mtime)).days > retention_days:
                    try:
                        size = os.path.getsize(file_path)
                        os.remove(file_path)
                        log_to_db(master_job_id, f"Deleted old archive: {file_path}")
                        deleted_count += 1
                        freed_space += size
                    except OSError as e:
                        log_to_db(master_job_id, f"Error deleting file {file_path}: {e}")

    log_to_db(master_job_id, f"Cleanup finished. Deleted {deleted_count} files, freeing {format_bytes(freed_space)}.")

# --- MAIN ORCHESTRATOR ---

def run_archive_job(selected_stack_paths, retention_days, archive_dir):
    """
    The main function to be run in a background thread.
    It orchestrates the entire archiving process for a list of stack paths.
    """
    # Create a "master" job entry for the overall process (mainly for cleanup logs)
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(
            "INSERT INTO archive_jobs (stack_name, start_time, status, log) VALUES (%s, %s, %s, %s) RETURNING id;",
            ('SYSTEM_JOB', datetime.now(), 'Running', 'Starting archiving process...\n')
        )
        master_job_id = cur.fetchone()['id']
        conn.commit()
    conn.close()

    for stack_path in selected_stack_paths:
        stack_name = os.path.basename(stack_path)
        start_time = datetime.now()
        job_id = None
        
        # Create a specific job entry for this stack
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "INSERT INTO archive_jobs (stack_name, start_time, status, log) VALUES (%s, %s, %s, %s) RETURNING id;",
                (stack_name, start_time, 'Running', f"Starting archive for stack: {stack_name}\n")
            )
            job_id = cur.fetchone()['id']
            conn.commit()
        conn.close()

        try:
            # Verify the stack path exists before proceeding
            if not os.path.isdir(stack_path):
                raise FileNotFoundError(f"Stack directory not found at path: {stack_path}")

            # 1. Stop Stack
            compose_action(stack_path, job_id, action="down")
            
            # 2. Archive Stack
            archive_path, archive_size = create_archive(stack_name, stack_path, archive_dir, job_id)
            
            # 3. Start Stack
            compose_action(stack_path, job_id, action="up")
            
            # 4. Update DB with Success
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE archive_jobs 
                    SET status = 'Success', end_time = %s, duration_seconds = %s, archive_path = %s, archive_size_bytes = %s, log = log || %s
                    WHERE id = %s;
                    """,
                    (end_time, int(duration), archive_path, archive_size, 'Archive completed successfully.', job_id)
                )
                conn.commit()
            conn.close()

        except Exception as e:
            # Mark job as failed on any error
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            error_log = f"FATAL: Archive failed for {stack_name}. Reason: {e}\n"
            log_to_db(job_id, error_log)
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE archive_jobs SET status = 'Failed', end_time = %s, duration_seconds = %s WHERE id = %s;",
                    (end_time, int(duration), job_id)
                )
                conn.commit()
            conn.close()
            # Attempt to restart the stack even on failure
            try:
                log_to_db(job_id, "Attempting to restart the stack after failure...")
                compose_action(stack_path, job_id, action="up")
            except Exception as restart_e:
                log_to_db(job_id, f"Could not restart stack after failure: {restart_e}")

    # After all stacks are processed, run cleanup
    try:
        cleanup_local_archives(archive_dir, retention_days, master_job_id)
        log_to_db(master_job_id, 'Archiving process finished.')
        status = 'Success'
    except Exception as e:
        log_to_db(master_job_id, f'Cleanup process failed: {e}')
        status = 'Failed'

    # Mark master job as complete
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE archive_jobs SET status = %s, end_time = %s WHERE id = %s;", (status, datetime.now(), master_job_id))
        conn.commit()
    conn.close()

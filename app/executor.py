"""
Archive execution engine with phased processing.
"""
import os
import subprocess
import time
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import logging
from app.db import get_db
from app import utils
from app.stacks import validate_stack, find_compose_file, discover_stacks
from app.utils import setup_logging, get_logger, get_archives_path, get_display_timezone
from app.sse import send_global_event
from app.notifications.helpers import get_setting
from app.notifications.handlers import send_archive_notification, send_archive_failure_notification

# Configure logging using centralized setup so LOG_LEVEL is respected
setup_logging()
logger = get_logger(__name__)
# SSE/event utilities (best-effort import; if missing, provide no-op)
try:
    from app.sse import send_event
except Exception:
    def send_event(job_id, event_type, payload):
        pass


ARCHIVE_BASE = get_archives_path()


# Registry of running executors (job_id -> executor instance)
RUNNING_EXECUTORS = {}


class ArchiveExecutor:
    """Handles archive job execution with phased processing."""
    
    def __init__(self, archive_config, is_dry_run=False, dry_run_config=None):
        """
        Initialize executor.
        
        Args:
            archive_config: Dict with archive configuration
            is_dry_run: Whether this is a simulation
            dry_run_config: Dict with dry run options (stop_containers, create_archive, run_retention)
        """
        self.config = archive_config
        self.is_dry_run = is_dry_run
        self.dry_run_config = dry_run_config or {}
        self.job_id = None
        self.log_buffer = []
        self.stack_host_paths = {}  # Cache for stack name -> host path mapping
    
    def _get_host_path_from_container(self, stack_name, stack_dir):
        """
        Get the host path for a stack directory.
        
        First tries to determine from mount configuration (DOCKER_STACK_PATHS),
        then falls back to inspecting running containers.
        
        Args:
            stack_name: Name of the stack
            stack_dir: Path to stack directory inside this container
            
        Returns:
            Tuple of (host_path, volume_names) where:
            - host_path: Host path for the stack directory
            - volume_names: List of named volumes found
        """
        # First try: Use mount configuration
        host_path = self._get_host_path_from_mount_config(stack_dir)
        if host_path != stack_dir:
            # If mount config resolved to a different (host) path, ensure it's accessible in container
            try:
                if not Path(str(host_path)).exists():
                    self.log('DEBUG', f"Mount config returned host path {host_path} which is not accessible in container; falling back to container inspection")
                    # Fall back to container inspection
                    return self._get_host_path_from_container_inspect(stack_name, stack_dir)
            except Exception:
                # If any error checking path, fallback to container inspection
                return self._get_host_path_from_container_inspect(stack_name, stack_dir)

            self.log('INFO', f"Found host path from mount config: {host_path}")
            # Still need to check for named volumes from containers
            named_volumes = self._get_named_volumes_from_container(stack_name)
            return host_path, named_volumes
        
        # Fallback: Try container inspection (old method)
        self.log('DEBUG', f"Mount config didn't help, trying container inspection")
        return self._get_host_path_from_container_inspect(stack_name, stack_dir)
    
    def _get_host_path_from_mount_config(self, container_path):
        """
        Get the host path by checking if container_path is in STACKS_DIR.
        Since we assume host and container paths are identical, we just verify
        the path is configured in STACKS_DIR.
        """
        from app.stacks import get_stack_mount_paths
        
        stack_paths = get_stack_mount_paths()
        container_path = Path(container_path)
        
        # Check if container_path is under any configured stack directory
        for stack_dir in stack_paths:
            stack_dir_path = Path(stack_dir)
            try:
                # Check if container_path is under this stack directory
                container_path.relative_to(stack_dir_path)
                # If we get here, it's under the stack directory
                # Since host and container paths are identical, return as-is
                return container_path
            except ValueError:
                continue
        
        # Not under any configured stack directory, return as-is (backward compatibility)
        return container_path
    
    def _get_host_path_from_container_inspect(self, stack_name, stack_dir):
        """
        Get the host path for a stack directory by inspecting running containers.
        
        This finds containers from the stack and checks their bind mounts to determine
        where the stack directory is located on the host. Also detects named volumes.
        
        Args:
            stack_name: Name of the stack
            stack_dir: Path to stack directory inside this container
            
        Returns:
            Tuple of (host_path, volume_names) where:
            - host_path: Host path for the stack directory
            - volume_names: List of named volumes found
        """
        try:
            # Get list of containers for this stack
            result = subprocess.run(
                ['docker', 'ps', '-q', '-f', f'label=com.docker.compose.project={stack_name}'],
                capture_output=True, text=True, timeout=10
            )
            
            if result.returncode != 0 or not result.stdout.strip():
                self.log('DEBUG', f"No running containers found for stack {stack_name}")
                return stack_dir, []
            
            container_ids = result.stdout.strip().split('\n')
            stack_dir_str = str(stack_dir)
            found_host_path = None
            named_volumes = []
            
            # Inspect containers to find bind mounts and named volumes
            for container_id in container_ids:
                inspect_result = subprocess.run(
                    ['docker', 'inspect', container_id],
                    capture_output=True, text=True, timeout=10
                )
                
                if inspect_result.returncode != 0:
                    continue
                
                inspect_data = json.loads(inspect_result.stdout)
                if not inspect_data:
                    continue
                
                container_data = inspect_data[0]
                mounts = container_data.get('Mounts', [])
                
                # Check all mounts
                for mount in mounts:
                    mount_type = mount.get('Type', '')
                    
                    # Collect named volumes
                    if mount_type == 'volume':
                        volume_name = mount.get('Name', '')
                        if volume_name and volume_name not in named_volumes:
                            named_volumes.append(volume_name)
                    
                    # Look for bind mounts to determine host path
                    elif mount_type == 'bind' and not found_host_path:
                        destination = mount.get('Destination', '')
                        source = mount.get('Source', '')
                        
                        # Check if the source path looks like it's under the stack directory
                        # e.g., Source="/opt/stacks/immich/library" suggests stack is at /opt/stacks/immich
                        if source and '/' in source:
                            source_parts = Path(source).parts
                            stack_dir_parts = Path(stack_dir_str).parts
                            
                            # Try to find common subpath pattern
                            # If destination is /usr/src/app/upload and source is /opt/stacks/immich/library,
                            # and stack_dir is a container-side path for the same stack, we can infer host stack is /opt/stacks/immich
                            for i in range(len(source_parts) - 1, 0, -1):
                                potential_host_stack = Path(*source_parts[:i])
                                potential_host_stack_name = potential_host_stack.name
                                
                                # Check if this looks like our stack directory
                                if potential_host_stack_name == stack_dir_parts[-1]:
                                    found_host_path = potential_host_stack
                                    break
            
            # Log findings
            if found_host_path:
                self.log('INFO', f"Found host path from container inspect: {found_host_path}")
            else:
                # Fallback: Try to determine host path from /proc/self/mountinfo
                self.log('DEBUG', f"No bind mounts found in containers, checking /proc/self/mountinfo")
                found_host_path = self._get_host_path_from_proc(stack_dir)

            # Ensure the found host path is accessible within the container; if not, fall back to container path
            try:
                if found_host_path and str(found_host_path) != str(stack_dir) and not Path(str(found_host_path)).exists():
                    self.log('DEBUG', f"Host path {found_host_path} is not accessible inside container; using container path {stack_dir}")
                    found_host_path = stack_dir
            except Exception:
                found_host_path = stack_dir
            
            if named_volumes:
                self.log('WARNING', f"⚠️  Stack {stack_name} uses named volumes: {', '.join(named_volumes)}")
                self.log('WARNING', f"    Named volumes are NOT included in the backup archive!")
                self.log('WARNING', f"    Consider using 'docker volume backup' or similar tools for volume data.")
            
            return found_host_path, named_volumes
            
        except Exception as e:
            self.log('WARNING', f"Error inspecting containers for host path: {e}")
            return stack_dir, []
    
    def _get_host_path_from_proc(self, container_path):
        """
        Fallback method to get host path by reading /proc/self/mountinfo.
        Used when no bind mounts are found in container inspection.
        
        Args:
            container_path: Path as seen inside this container
            
        Returns:
            Host path if found, otherwise returns container_path unchanged
        """
        container_path = Path(container_path).resolve()
        container_path_str = str(container_path)
        
        try:
            with open('/proc/self/mountinfo', 'r') as f:
                mounts = []
                for line in f:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    
                    # Skip special filesystems
                    fs_type = parts[8] if len(parts) > 8 else ''
                    if fs_type in ['overlay', 'tmpfs', 'proc', 'sysfs', 'devpts', 'devtmpfs', 'cgroup', 'cgroup2']:
                        continue
                    
                    mount_point = parts[4]  # Where it's mounted in container
                    
                    # Find the source field (after the '-' separator)
                    separator_idx = parts.index('-') if '-' in parts else -1
                    if separator_idx > 0 and len(parts) > separator_idx + 2:
                        source = parts[separator_idx + 2]
                        mounts.append((mount_point, source))
                
                # Sort by mount point length (longest first) to match most specific path
                mounts.sort(key=lambda x: len(x[0]), reverse=True)
                
                # Find matching mount
                for mount_point, source in mounts:
                    if container_path_str.startswith(mount_point):
                        # Calculate relative path from mount point
                        relative = container_path_str[len(mount_point):].lstrip('/')
                        # Combine with host source
                        host_path = Path(source) / relative if relative else Path(source)
                        self.log('INFO', f"Found host path from /proc/self/mountinfo: {host_path}")
                        return host_path
                
                # No matching mount found
                self.log('DEBUG', f"No mount mapping found in /proc/self/mountinfo for {container_path}")
                return container_path
                
        except Exception as e:
            self.log('WARNING', f"Could not read /proc/self/mountinfo: {e}")
            return container_path
    
    def _get_named_volumes_from_container(self, stack_name):
        """
        Get named volumes used by containers in a stack.
        
        Args:
            stack_name: Name of the stack
            
        Returns:
            List of named volume names
        """
        try:
            # Get list of containers for this stack
            result = subprocess.run(
                ['docker', 'ps', '-q', '-f', f'label=com.docker.compose.project={stack_name}'],
                capture_output=True, text=True, timeout=10
            )
            
            if result.returncode != 0 or not result.stdout.strip():
                return []
            
            container_ids = result.stdout.strip().split('\n')
            named_volumes = []
            
            # Inspect containers to find named volumes
            for container_id in container_ids:
                inspect_result = subprocess.run(
                    ['docker', 'inspect', container_id],
                    capture_output=True, text=True, timeout=10
                )
                
                if inspect_result.returncode != 0:
                    continue
                
                inspect_data = json.loads(inspect_result.stdout)
                if not inspect_data:
                    continue
                
                container_data = inspect_data[0]
                mounts = container_data.get('Mounts', [])
                
                # Collect named volumes
                for mount in mounts:
                    mount_type = mount.get('Type', '')
                    if mount_type == 'volume':
                        volume_name = mount.get('Name', '')
                        if volume_name and volume_name not in named_volumes:
                            named_volumes.append(volume_name)
            
            return named_volumes
            
        except Exception as e:
            self.log('WARNING', f"Error inspecting containers for named volumes: {e}")
            return []
    
    def log(self, level, message):
        """Add log entry with timestamp and persist to DB for live tailing across processes.

        Logs are appended to the in-memory buffer (for fast local access) and also
        appended to the jobs.log column in the database so other web worker
        processes can stream the log while the job is still running.
        """
        timestamp = utils.local_now().strftime('%Y-%m-%d %H:%M:%S')
        prefix = "[SIMULATION] " if self.is_dry_run else ""
        log_line = f"[{timestamp}] [{level}] {prefix}{message}"
        # Append to in-memory buffer
        self.log_buffer.append(log_line)
        # Emit to logger (and let logging handlers decide where to write)
        logger.info(log_line)

        # Emit SSE event for live listeners (best-effort)
        try:
            if self.job_id:
                send_event(self.job_id, 'log', {'line': log_line})
        except Exception:
            pass

        # Also persist incrementally to the DB (append). Guard against any DB errors.
        try:
            if self.job_id:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE jobs
                        SET log = log || %s
                        WHERE id = %s;
                    """, (log_line + "\n", self.job_id))
                    conn.commit()
        except Exception:
            # Don't let logging failures interrupt the job
            pass

    def run(self, triggered_by='manual', job_id=None):
        """Execute archive job with all phases.

        If ``job_id`` is provided, use the existing job record instead of creating
        a new one (useful when the API pre-creates the job and spawns a detached subprocess).
        """
        start_time = utils.now()
        self.log('INFO', f"Starting archive job for: {self.config['name']}")
        
        # Use provided job_id if present, otherwise create a new job record
        if job_id:
            self.job_id = job_id
        else:
            self.job_id = self._create_job_record(start_time, triggered_by)

        # Register executor for live log access
        try:
            RUNNING_EXECUTORS[self.job_id] = self
        except Exception:
            pass

        # Quick sanity check: ensure at least one configured stack resolves to a valid path
        try:
            valid_stacks = []
            for s in self.config.get('stacks', []):
                try:
                    if self._find_stack_path(s):
                        valid_stacks.append(s)
                except Exception:
                    # Ignore errors while resolving
                    continue
            if not valid_stacks:
                self.log('ERROR', 'No valid stacks found for this archive — bind mounts are mandatory and host:container paths must be identical. Aborting job. See README "How Stack Discovery Works" and dashboard warnings for troubleshooting.')
                self._update_job_status('failed', error='No valid stacks found (bind mounts mandatory; host:container paths must match)')
                return self.job_id
        except Exception:
            pass

        try:
            # Phase 0: Initialize directories
            self._phase_0_init()
            
            # Phase 1: Process stacks sequentially
            stack_metrics = self._phase_1_process_stacks()
            
            # Phase 2: Run retention (if configured and not disabled in dry run)
            if self._should_run_retention():
                self._phase_2_retention()
            
            # Phase 3: Finalize and notify
            self._phase_3_finalize(start_time, stack_metrics)
            
            return self.job_id
            
        except Exception as e:
            self.log('ERROR', f"Archive job failed: {str(e)}")
            self._update_job_status('failed', error=str(e))
            raise
        finally:
            # Ensure we deregister executor when finished so API stops pulling live logs
            try:
                if self.job_id and self.job_id in RUNNING_EXECUTORS:
                    del RUNNING_EXECUTORS[self.job_id]
            except Exception:
                pass

    def _create_job_record(self, start_time, triggered_by):
        """Create initial job record in database (inline implementation)."""
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO jobs (
                    archive_id, job_type, status, start_time, 
                    is_dry_run, dry_run_config, triggered_by, log
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) 
                RETURNING id;
            """, (
                self.config['id'], 'archive', 'running', start_time,
                self.is_dry_run, json.dumps(self.dry_run_config) if self.dry_run_config else None,
                triggered_by, ''
            ))
            job_id = cur.fetchone()['id']
            conn.commit()
            return job_id


def get_running_executor(job_id):
    """Return a running ArchiveExecutor instance for a job id, if available."""
    return RUNNING_EXECUTORS.get(job_id)


def _create_job_record_impl(self, start_time, triggered_by):
    """Module-level implementation of job record creation (bound to class for safety)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO jobs (
                archive_id, job_type, status, start_time, 
                is_dry_run, dry_run_config, triggered_by, log
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) 
            RETURNING id;
        """, (
            self.config['id'], 'archive', 'running', start_time,
            self.is_dry_run, json.dumps(self.dry_run_config) if self.dry_run_config else None,
            triggered_by, ''
        ))
        job_id = cur.fetchone()['id']
        conn.commit()

        # Broadcast a global job creation event so dashboards can update in real-time
        try:
            job_meta = {
                'id': job_id,
                'archive_id': self.config['id'],
                'archive_name': self.config.get('name'),
                'job_type': 'archive',
                'status': 'running',
                'start_time': start_time,
                'is_dry_run': self.is_dry_run,
                'stack_names': ','.join(self.config.get('stacks', []))
            }
            send_global_event('job', job_meta)
            try:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.info("[SSE] Global event SENT (job create) id=%s archive_id=%s start_time=%s", job_id, self.config['id'], start_time)
            except Exception:
                pass
        except Exception:
            pass

        return job_id

# Bind implementation to class so instances always have the method
ArchiveExecutor._create_job_record = _create_job_record_impl

    
def _phase_0_init(self):
        """Phase 0: Initialize directories."""
        self.log('INFO', '### Phase 0: Initializing directories ###')
        
        archive_name = self.config['name']
        base_dir = Path(ARCHIVE_BASE) / archive_name
        
        if not self.is_dry_run:
            base_dir.mkdir(parents=True, exist_ok=True)
            self.log('INFO', f"Ensured archive directory exists: {base_dir}")
        else:
            self.log('INFO', f"Would ensure archive directory exists: {base_dir}")
    
def _phase_1_process_stacks(self):
        """Phase 1: Process each stack sequentially."""
        self.log('INFO', '### Phase 1: Processing stacks sequentially (Stop -> Archive -> Start) ###')
        
        stack_metrics = []
        stacks = self.config['stacks']
        stop_containers = self.config.get('stop_containers', True)
        
        # Override with dry run config if present
        if self.is_dry_run:
            stop_containers = self.dry_run_config.get('stop_containers', stop_containers)
        
        for stack_name in stacks:
            metric = self._process_single_stack(stack_name, stop_containers)
            stack_metrics.append(metric)
        
        return stack_metrics
    
def _process_single_stack(self, stack_name, stop_containers):
        """Process a single stack: stop -> archive -> start."""
        stack_start = utils.now()
        self.log('INFO', f"--- Starting backup for stack: {stack_name} ---")
        
        # Find stack directory
        stack_path = self._find_stack_path(stack_name)
        if not stack_path:
            error_msg = f"Stack directory not found: {stack_name}"
            self.log('ERROR', error_msg)
            return self._create_stack_metric(stack_name, 'failed', stack_start, error=error_msg)
        
        # Validate compose file exists
        valid, error_msg = validate_stack(stack_path)
        if not valid:
            self.log('ERROR', error_msg)
            return self._create_stack_metric(stack_name, 'failed', stack_start, error=error_msg)
        
        compose_file = find_compose_file(stack_path)
        compose_path = Path(stack_path) / compose_file
        
        # Check if stack is currently running
        was_running = self._is_stack_running(stack_name, stack_path)
        self.log('INFO', f"Stack {stack_name} current state: {'running' if was_running else 'stopped'}")
        
        # Stop stack if needed and running
        if stop_containers and was_running:
            if not self._stop_stack(stack_name, compose_path):
                error_msg = f"Failed to stop stack: {stack_name}"
                return self._create_stack_metric(stack_name, 'failed', stack_start, was_running, error=error_msg)
        elif not stop_containers:
            self.log('WARNING', f"Creating archive without stopping stack {stack_name} - may result in inconsistent backup")
        elif not was_running:
            self.log('INFO', f"Stack {stack_name} is already stopped, skipping stop step")
        
        # Create archive
        archive_path, archive_size = self._create_archive(stack_name, stack_path)
        if not archive_path:
            error_msg = f"Failed to create archive for {stack_name}"
            return self._create_stack_metric(stack_name, 'failed', stack_start, was_running, error=error_msg)
        
        # Start stack if it was running and we stopped it
        start_result = None
        if stop_containers and was_running:
            start_result = self._start_stack(stack_name, compose_path)
            if start_result == 'skipped':
                # Intentionally skipped due to missing images (pull disabled); record and continue
                skip_reason = (getattr(self, 'stack_skip_reasons', {}) or {}).get(stack_name, 'Skipped starting stack (images missing).')
                self.log('WARNING', f"Restart skipped for {stack_name}: {skip_reason}")
                # Return a 'skipped' metric explicitly
                return self._create_stack_metric(stack_name, 'skipped', stack_start, was_running, error=skip_reason)
            if not start_result:
                # Pull or start failed — mark this stack as failed and mark job as failed
                error_msg = f"Failed to restart stack: {stack_name}"
                self.log('ERROR', error_msg)
                try:
                    self.job_failed = True
                except Exception:
                    pass
                # Return a 'failed' metric for this stack (archive was created earlier)
                return self._create_stack_metric(stack_name, 'failed', stack_start, was_running, archive_path=archive_path, archive_size=archive_size, error=error_msg)
        
        stack_end = utils.now()
        duration = int((stack_end - stack_start).total_seconds())
        
        self.log('INFO', f"--- Finished backup for stack: {stack_name} ---")
        
        return self._create_stack_metric(
            stack_name, 'success', stack_start, was_running,
            archive_path=archive_path, archive_size=archive_size, duration=duration
        )
    
def _find_stack_path(self, stack_name):
        """Find the full path for a stack by name."""
        from app.stacks import discover_stacks
        stacks = discover_stacks()
        for stack in stacks:
            if stack['name'] == stack_name:
                return stack['path']
        return None
    
def _is_stack_running(self, stack_name, stack_path):
        """Check if any containers in the stack are running."""
        try:
            compose_file = find_compose_file(stack_path)
            if not compose_file:
                self.log('WARNING', f"No compose file found for {stack_name}")
                return False
            
            # Use docker compose ps to check stack status
            result = subprocess.run(
                ['docker', 'compose', '-f', compose_file, 'ps', '-q'],
                cwd=stack_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            # If there are container IDs, check if any are running
            if result.returncode == 0 and result.stdout.strip():
                container_ids = result.stdout.strip().split('\n')
                for container_id in container_ids:
                    inspect_result = subprocess.run(
                        ['docker', 'inspect', '-f', '{{.State.Running}}', container_id],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if inspect_result.returncode == 0 and inspect_result.stdout.strip() == 'true':
                        return True
                return False
            
            containers = []
            return len(containers) > 0
        except Exception as e:
            self.log('WARNING', f"Could not check running state for {stack_name}: {e}")
            return False
    
def _stop_stack(self, stack_name, compose_path):
        """Stop a docker compose stack."""
        stack_dir = compose_path.parent
        self.log('INFO', f"Stopping stack in {stack_dir}...")
        
        # Inspect containers before stopping to:
        # 1. Detect named volumes for warnings
        # 2. Find host path for later restart
        host_stack_dir, named_volumes = self._get_host_path_from_container(stack_name, stack_dir)
        
        # Cache the host path and volumes for use when restarting and reporting
        self.stack_host_paths[stack_name] = host_stack_dir
        if named_volumes:
            if not hasattr(self, 'stack_volumes'):
                self.stack_volumes = {}
            self.stack_volumes[stack_name] = named_volumes
        
        # Execute docker compose in the host stack directory
        # This way .env and compose.yml are automatically found
        cmd_parts = ['docker', 'compose', 'down']
        self.log('INFO', f"Starting command: Stopping {stack_name} (docker compose down)")
        
        if self.is_dry_run:
            self.log('INFO', f"Would execute in {host_stack_dir}: {' '.join(cmd_parts)}")
            return True
        
        try:
            result = subprocess.run(
                cmd_parts, 
                cwd=str(host_stack_dir),  # Execute in host stack directory (now mounted as /opt/stacks)
                capture_output=True, 
                text=True, 
                timeout=120
            )
            if result.returncode == 0:
                self.log('INFO', f"Successfully finished: Stopping {stack_name}")
                return True
            else:
                self.log('ERROR', f"Failed to stop {stack_name}: {result.stderr}")
                return False
        except Exception as e:
            self.log('ERROR', f"Exception stopping {stack_name}: {e}")
            return False
    
def _start_stack(self, stack_name, compose_path):
        """Start a docker compose stack."""
        stack_dir = compose_path.parent
        self.log('INFO', f"Starting stack in {stack_dir}...")
        
        # Use cached host path from when we stopped the stack
        # If not cached (e.g., stack wasn't running), use container path
        host_stack_dir = self.stack_host_paths.get(stack_name, stack_dir)
        
        # Execute docker compose in the host stack directory
        # Docker Compose will automatically:
        # - Load .env file from current directory
        # - Find compose.yml/docker-compose.yml in current directory
        # - Load compose.override.yml if it exists
        # Before starting, check whether images referenced in the compose file are available locally.
        policy = get_setting('image_pull_policy', 'never').lower()
        # Policy values: 'never' | 'always' (use checkbox in settings). Pulls use an inactivity timeout configured by 'image_pull_inactivity_timeout'.
        missing_images = []
        try:
            # Try to get images from 'docker compose config --format json'
            import json as _json
            cf = subprocess.run(['docker', 'compose', '-f', str(compose_path), 'config', '--format', 'json'], cwd=str(host_stack_dir), capture_output=True, text=True, timeout=30)
            services = []
            if cf.returncode == 0 and cf.stdout:
                try:
                    cfg = _json.loads(cf.stdout)
                    services = cfg.get('services', {})
                    images = [s.get('image') for s in services.values() if isinstance(s, dict) and s.get('image')]
                except Exception:
                    images = []
            else:
                # Fallback: parse plain config output for 'image:' lines
                cf2 = subprocess.run(['docker', 'compose', '-f', str(compose_path), 'config'], cwd=str(host_stack_dir), capture_output=True, text=True, timeout=30)
                images = []
                if cf2.returncode == 0 and cf2.stdout:
                    for line in cf2.stdout.splitlines():
                        line = line.strip()
                        if line.startswith('image:'):
                            parts = line.split(None, 1)
                            if len(parts) == 2:
                                images.append(parts[1].strip())
            # Check each image via docker SDK
            try:
                import docker as _docker
                client = _docker.from_env()
                for img in images:
                    try:
                        client.images.get(img)
                    except Exception:
                        missing_images.append(img)
            except Exception:
                # Unable to check images (docker not available); assume none missing
                missing_images = []
        except Exception as e:
            self.log('WARNING', f"Failed to determine images for {stack_name}: {e}")
            missing_images = []

        # Track whether we executed an explicit pull to avoid duplicate pulls
        pull_executed = False
        # Track whether we attempted an explicit pull (success or failure)
        pull_attempted = False

        # Handle 'always' policy: pull regardless of missingImages
        if policy == 'always':
            self.log('INFO', f"Pull policy is 'always' — attempting to pull images for {stack_name} before starting.")
            try:
                # Stream pull output using Popen so we can capture full raw output while
                # still enforcing a timeout and avoiding blocking IO issues.
                import threading

                def _drain_pipe(pipe, out_list):
                    try:
                        for line in iter(pipe.readline, ''):
                            out_list.append(line)
                    except Exception:
                        pass
                    finally:
                        try:
                            pipe.close()
                        except Exception:
                            pass

                pull_attempted = True
                popen_proc = subprocess.Popen(
                    ['docker', 'compose', '-f', str(compose_path), 'pull'],
                    cwd=str(host_stack_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )

                stdout_lines = []
                stderr_lines = []
                # Track last activity timestamp so we can implement an inactivity-based timeout
                last_activity = {'t': time.time()}

                def _drain_pipe_track(pipe, out_list):
                    try:
                        for line in iter(pipe.readline, ''):
                            out_list.append(line)
                            # Update last activity whenever data arrives
                            try:
                                last_activity['t'] = time.time()
                            except Exception:
                                pass
                    except Exception:
                        pass
                    finally:
                        try:
                            pipe.close()
                        except Exception:
                            pass

                t_out = threading.Thread(target=_drain_pipe_track, args=(popen_proc.stdout, stdout_lines), daemon=True)
                t_err = threading.Thread(target=_drain_pipe_track, args=(popen_proc.stderr, stderr_lines), daemon=True)
                t_out.start()
                t_err.start()

                # Determine inactivity timeout (0 = disabled)
                try:
                    inactivity_timeout = int(get_setting('image_pull_inactivity_timeout', '300'))
                except Exception:
                    inactivity_timeout = 300

                # Wait until process exits or inactivity timeout elapses
                try:
                    while True:
                        if popen_proc.poll() is not None:
                            break
                        if inactivity_timeout and inactivity_timeout > 0 and (time.time() - last_activity['t']) > inactivity_timeout:
                            # Kill the process and capture partial output
                            try:
                                popen_proc.kill()
                            except Exception:
                                pass
                            try:
                                popen_proc.wait(timeout=5)
                            except Exception:
                                pass
                            t_out.join(timeout=1)
                            t_err.join(timeout=1)
                            pull_output = (''.join(stdout_lines) or '').strip() + '\n' + (''.join(stderr_lines) or '').strip()
                            try:
                                if not hasattr(self, 'stack_image_updates'):
                                    self.stack_image_updates = {}
                                self.stack_image_updates[stack_name] = {'pull_output': pull_output}
                            except Exception:
                                pass
                            self.log('WARNING', f"Image pull timed out after {inactivity_timeout}s of inactivity; aborting pull for {stack_name} — check network/registry")
                            return False
                        time.sleep(0.2)
                except Exception:
                    # If any error occurred waiting, attempt to kill and record output
                    try:
                        popen_proc.kill()
                    except Exception:
                        pass
                    t_out.join(timeout=1)
                    t_err.join(timeout=1)
                    pull_output = (''.join(stdout_lines) or '').strip() + '\n' + (''.join(stderr_lines) or '').strip()
                    try:
                        if not hasattr(self, 'stack_image_updates'):
                            self.stack_image_updates = {}
                        self.stack_image_updates[stack_name] = {'pull_output': pull_output}
                    except Exception:
                        pass
                    self.log('WARNING', f"An error occurred while waiting for image pull for {stack_name}; aborting")
                    return False

                # Ensure drain threads have finished
                t_out.join(timeout=1)
                t_err.join(timeout=1)

                # Non-zero returncode indicates pull failure
                if popen_proc.returncode != 0:
                    pull_output = (''.join(stdout_lines) or '').strip() + '\n' + (''.join(stderr_lines) or '').strip()
                    self.log('WARNING', f"We couldn't pull the required images for {stack_name}: {pull_output}")
                    return False

                # Mark that explicit pull succeeded
                pull_executed = True

                pull_output = (''.join(stdout_lines) or '').strip() + '\n' + (''.join(stderr_lines) or '').strip()
                try:
                    if not hasattr(self, 'stack_image_updates'):
                        self.stack_image_updates = {}
                    self.stack_image_updates[stack_name] = {'pull_output': pull_output}
                except Exception:
                    pass

                self.log('INFO', f"Container images pulled for {stack_name}; check the pull output in the job log for details.")
                # Log the pull command output at DEBUG for operators
                if pull_output:
                    self.log('DEBUG', f"Pull output for {stack_name}:\n{pull_output}")
                # Re-check existence
                try:
                    import docker as _docker2
                    client = _docker2.from_env()
                    still_missing = []
                    for img in images:
                        try:
                            client.images.get(img)
                        except Exception:
                            still_missing.append(img)
                    if still_missing:
                        self.log('WARNING', f"Some images remain unavailable after attempting to pull for {stack_name}: {', '.join(still_missing)}")
                        return False
                except Exception:
                    pass
            except Exception as e:
                self.log('WARNING', f"An error occurred while attempting to pull images for {stack_name}: {e}")
                return False

        # If there are missing images and policy is not 'always', skip the stack
        if missing_images:
            reason = f"Skipped starting stack {stack_name} because required images were not available locally and pull policy is set to 'never'. See README for details."
            self.log('WARNING', reason)
            # Record skip reason so notifications can include it via stack metric
            if not hasattr(self, 'stack_skip_reasons'):
                self.stack_skip_reasons = {}
            self.stack_skip_reasons[stack_name] = reason
            return 'skipped'

        # Build docker compose up command; optionally append a pull policy flag when supported
        cmd_parts = ['docker', 'compose', 'up']
        if policy == 'never':
            # Always enforce no-pull via CLI so we do not accidentally pull when starting stacks
            cmd_parts.append('--pull=never')
            self.log('INFO', f"Starting {stack_name} without pulling images because pull policy is set to 'never'.")
        elif policy == 'always':
            # We always prefer an explicit 'docker compose pull' and will never add
            # '--pull=always' to 'docker compose up' to avoid duplicate pulls and
            # unnecessary additional network load.
            # No further action required here.
            pass
        cmd_parts.append('-d')
        self.log('INFO', f"Starting command: Starting {stack_name} ({' '.join(cmd_parts)})")
        
        if self.is_dry_run:
            self.log('INFO', f"Would execute in {host_stack_dir}: {' '.join(cmd_parts)}")
            return True
        
        try:
            result = subprocess.run(
                cmd_parts,
                cwd=str(host_stack_dir),  # Execute in host stack directory (now mounted as /opt/stacks)
                capture_output=True,
                text=True,
                timeout=120
            )
            if result.returncode == 0:
                self.log('INFO', f"Successfully finished: Starting {stack_name}")
                return True
            else:
                self.log('ERROR', f"Failed to start {stack_name}: {result.stderr}")
                return False
        except Exception as e:
            self.log('ERROR', f"Exception starting {stack_name}: {e}")
            return False
    
def _create_archive(self, stack_name, stack_path):
        """Create archive of stack directory."""
        timestamp = utils.local_now().strftime('%Y%m%d_%H%M%S')
        output_format = self.config.get('output_format', 'tar')
        archive_name = self.config['name']
        
        # Determine file extension and compression
        if output_format == 'tar.gz':
            ext = 'tar.gz'
            tar_opts = '-czf'
        elif output_format == 'tar.zst':
            ext = 'tar.zst'
            tar_opts = '--use-compress-program=zstd -cf'
        elif output_format == 'folder':
            ext = None  # No archive, just copy directory
        else:  # tar (uncompressed)
            ext = 'tar'
            tar_opts = '-cf'
        
        # Create output paths
        base_dir = Path(ARCHIVE_BASE) / archive_name
        # Use a per-stack directory for all outputs (packed files and folder copies)
        stack_dir = base_dir / stack_name
        output_dir = stack_dir
        # For auditing, log the timestamp and configured display timezone
        try:
            tz_obj = get_display_timezone()
            tz_name = getattr(tz_obj, 'key', None) or os.environ.get('TZ', 'UTC')
            aware_local = datetime.now(tz_obj)
            iso_local = aware_local.isoformat()
            iso_utc = aware_local.astimezone(timezone.utc).isoformat()
        except Exception:
            tz_name = os.environ.get('TZ', 'UTC')
            iso_local = timestamp
            iso_utc = ''
        # Keep the job log message concise and user friendly
        self.log('INFO', f"Archive timestamp: {timestamp}")
        
        if ext:
            # Store packed archives inside the stack folder as <timestamp>_<stackname>.<ext>
            output_file = output_dir / f"{timestamp}_{stack_name}.{ext}"
        else:
            # For folder outputs, place the timestamped folder inside the per-stack folder as <timestamp>_<stackname>
            output_file = output_dir / f"{timestamp}_{stack_name}"
        
        # Skip archive creation if disabled in dry run
        if self.is_dry_run and not self.dry_run_config.get('create_archive', True):
            self.log('INFO', f"Skipping archive creation for '{stack_name}' (dry run disabled)")
            return str(output_file), 0
        
        # Ensure parent directories exist
        if not self.is_dry_run:
            # Ensure the per-stack output directory exists
            output_dir.mkdir(parents=True, exist_ok=True)
            # If configured, apply directory permissions (0755) for the output directory
            try:
                from app.notifications.helpers import get_setting
                if get_setting('apply_permissions', 'false').lower() == 'true':
                    try:
                        output_dir.chmod(0o755)
                    except Exception as pe:
                        self.log('DEBUG', f"Could not chmod output directory {output_dir}: {pe}")
            except Exception:
                pass        
        if ext:
            # Create compressed archive
            format_name = output_format.upper()
            self.log('INFO', f"Creating {format_name} archive for '{stack_name}' at {output_file}...")
            
            parent_dir = Path(stack_path).parent
            stack_dirname = Path(stack_path).name
            
            # Build tar command as array for security
            cmd_parts = ['tar']
            if tar_opts:
                cmd_parts.extend(tar_opts.split())
            cmd_parts.extend([str(output_file), '-C', str(parent_dir), stack_dirname])
            
            self.log('INFO', f"Starting command: Archiving {stack_name} (tar)")
            
            if self.is_dry_run:
                self.log('INFO', f"Would execute: tar {tar_opts} {output_file} -C {parent_dir} {stack_dirname}")
                return str(output_file), 0
            
            try:
                result = subprocess.run(
                    cmd_parts, capture_output=True, text=True, timeout=600
                )
                if result.returncode != 0:
                    self.log('ERROR', f"Failed to create archive: {result.stderr}")
                    return None, 0
                
                self.log('INFO', f"Successfully finished: Archiving {stack_name}")
                
                # Ensure archive file is world-readable so downstream backup tools (e.g., Borg) can access it
                try:
                    apply_perms = False
                    try:
                        from app.notifications.helpers import get_setting
                        apply_perms = get_setting('apply_permissions', 'false').lower() == 'true'
                    except Exception:
                        apply_perms = False

                    if apply_perms:
                        output_file.chmod(0o644)
                        # Log permission change to the job log for observability
                        self.log('INFO', f"Set archive permissions to 0644 for {output_file}")
                    else:
                        # Skipping permission changes due to settings; not logged at INFO to avoid noise
                        self.log('DEBUG', "Skipping chmod for archive files due to settings (apply_permissions disabled)")
                except Exception as pe:
                    # Some filesystems (e.g., certain mounts) may not support chmod — log at DEBUG and continue
                    self.log('DEBUG', f"Could not chmod archive {output_file}: {pe}")

                # Get archive size
                archive_size = output_file.stat().st_size
                archive_size_mb = archive_size / (1024 * 1024)
                self.log('INFO', f"Archive created successfully for {stack_name}. Size: {archive_size_mb:.1f}M ({archive_size} bytes).")
                
                return str(output_file), archive_size
                
            except Exception as e:
                self.log('ERROR', f"Exception creating archive: {e}")
                return None, 0
        else:
            # Copy as folder
            self.log('INFO', f"Copying '{stack_name}' as folder to {output_file}...")
            
            cmd_parts = ['cp', '-r', str(stack_path), str(output_file)]
            self.log('INFO', f"Starting command: Copying {stack_name} (cp -r)")
            
            if self.is_dry_run:
                self.log('INFO', f"Would execute: cp -r {stack_path} {output_file}")
                return str(output_file), 0
            
            try:
                result = subprocess.run(
                    cmd_parts, capture_output=True, text=True, timeout=300
                )
                if result.returncode != 0:
                    self.log('ERROR', f"Failed to copy folder: {result.stderr}")
                    return None, 0
                
                # Calculate folder size
                result = subprocess.run(
                    ['du', '-sb', str(output_file)], capture_output=True, text=True
                )
                folder_size = int(result.stdout.split()[0]) if result.returncode == 0 else 0
                folder_size_mb = folder_size / (1024 * 1024)

                # Ensure copied folder contents are readable by backup tools (make files 0644 and dirs 0755) if enabled in settings
                success_dirs = success_files = fail_dirs = fail_files = 0
                try:
                    apply_perms = False
                    try:
                        from app.notifications.helpers import get_setting
                        apply_perms = get_setting('apply_permissions', 'false').lower() == 'true'
                    except Exception:
                        apply_perms = False

                    if apply_perms:
                        for root, dirs, files in os.walk(str(output_file)):
                            for d in dirs:
                                try:
                                    os.chmod(os.path.join(root, d), 0o755)
                                    success_dirs += 1
                                except Exception:
                                    fail_dirs += 1
                            for f in files:
                                try:
                                    os.chmod(os.path.join(root, f), 0o644)
                                    success_files += 1
                                except Exception:
                                    fail_files += 1
                    else:
                        # Skipping permission walk due to settings; not logged at INFO to avoid noisy logs
                        self.log('DEBUG', f"Skipping permission adjustments for copied folder {output_file} due to settings (apply_permissions disabled)")
                except Exception as pe:
                    # If the walk itself fails, log at DEBUG and continue
                    self.log('DEBUG', f"Permission walk failed for {output_file}: {pe}")

                # Log a concise summary to the job log
                self.log('INFO', f"Adjusted permissions for copied folder: dirs_ok={success_dirs}, files_ok={success_files}, dirs_failed={fail_dirs}, files_failed={fail_files}")
                if fail_dirs or fail_files:
                    self.log('DEBUG', f"Some chmod operations failed (ignored) for {output_file}: dirs_failed={fail_dirs}, files_failed={fail_files}")
                
                self.log('INFO', f"Folder created successfully for {stack_name}. Size: {folder_size_mb:.1f}M ({folder_size} bytes).")
                
                return str(output_file), folder_size
                
            except Exception as e:
                self.log('ERROR', f"Exception copying folder: {e}")
                return None, 0
    
def _should_run_retention(self):
        """Check if retention should run."""
        if self.is_dry_run:
            return self.dry_run_config.get('run_retention', True)
        return True
    
def _phase_2_retention(self):
        """Phase 2: Run retention cleanup."""
        self.log('INFO', '### Phase 2: Running local retention cleanup ###')
        
        from app.retention import run_retention
        
        try:
            result = run_retention(
                self.config, 
                self.job_id, 
                is_dry_run=self.is_dry_run,
                log_callback=self.log
            )
            reclaimed_bytes = result.get('reclaimed') if isinstance(result, dict) else result
            deleted = result.get('deleted') if isinstance(result, dict) else 0
            deleted_dirs = result.get('deleted_dirs') if isinstance(result, dict) else 0
            deleted_files = result.get('deleted_files') if isinstance(result, dict) else 0

            # Update job with reclaimed bytes and deleted counts
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE jobs SET reclaimed_bytes = %s, deleted_count = %s, deleted_dirs = %s, deleted_files = %s WHERE id = %s;",
                    (reclaimed_bytes, deleted, deleted_dirs, deleted_files, self.job_id)
                )
                conn.commit()
                
        except Exception as e:
            self.log('ERROR', f"Retention failed: {e}")
    
def _phase_3_finalize(self, start_time, stack_metrics):
        """Phase 3: Finalize job and send notifications."""
        self.log('INFO', '### Phase 3: Finalizing report and sending notification ###')
        
        # Get disk usage stats
        self._log_disk_usage()
        
        # Calculate totals
        total_size = sum(m['archive_size_bytes'] for m in stack_metrics)
        end_time = utils.now()
        duration = int((end_time - start_time).total_seconds())
        
        # Determine overall job status (if any stack failed or job_failed flag set)
        job_failed = getattr(self, 'job_failed', False) or any(m.get('status') == 'failed' for m in (stack_metrics or []))

        # Update job record
        if job_failed:
            # Build brief error message summarizing failures
            failed = [m for m in (stack_metrics or []) if m.get('status') == 'failed']
            try:
                error_msg = 'Stack failures: ' + ', '.join(f"{m.get('stack_name')}: {m.get('error') or 'failed'}" for m in failed)
            except Exception:
                error_msg = 'One or more stacks failed during restart'
            self._update_job_status('failed', end_time=end_time, duration=duration, total_size=total_size, error=error_msg)
        else:
            self._update_job_status('success', end_time=end_time, duration=duration, total_size=total_size)
        
        # Save stack metrics
        self._save_stack_metrics(stack_metrics)
        
        # Send notification (failure notification if job failed)
        if not self.is_dry_run:
            try:
                if job_failed:
                    logger.info("Notifications: invoking send_archive_failure_notification for archive=%s job=%s", self.config.get('name'), self.job_id)
                    send_archive_failure_notification(self.config, self.job_id, stack_metrics, duration, total_size)
                else:
                    self._send_notification(stack_metrics, duration, total_size)
            except Exception as e:
                self.log('WARNING', f"Failed to send notification: {e}")
        else:
            self.log('INFO', 'Would send notification (dry run)')
        
        if job_failed:
            self.log('ERROR', f"Archive job completed with failures in {duration}s")
        else:
            self.log('INFO', f"Archive job completed successfully in {duration}s")
    
def _log_disk_usage(self):
        """Log disk usage for archives directory."""
        cmd_parts = ['df', '-h', '--output=size,used,avail,pcent,target', ARCHIVE_BASE]
        self.log('INFO', f"Checking disk usage...")
        
        try:
            result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                output = result.stdout.strip()
                if output:
                    # Format as single line for cleaner log
                    lines = output.split('\n')
                    if len(lines) >= 2:
                        # Parse header and data
                        header = lines[0].split()
                        data = lines[1].split()
                        # Create readable single line
                        self.log('INFO', f"Disk usage: {data[0]} total, {data[1]} used, {data[2]} available ({data[3]} used) on {data[4]}")
                    else:
                        self.log('INFO', output)
            else:
                self.log('WARNING', f"Could not get disk usage: {result.stderr}")
        except Exception as e:
            self.log('WARNING', f"Exception checking disk usage: {e}")
    
def _create_stack_metric(self, stack_name, status, start_time, was_running=None, 
                            archive_path=None, archive_size=0, duration=0, error=None):
        """Create stack metric dict."""
        # Check if stack has named volumes
        named_volumes = None
        if hasattr(self, 'stack_volumes') and stack_name in self.stack_volumes:
            named_volumes = self.stack_volumes[stack_name]
        
        metric = {
            'stack_name': stack_name,
            'status': status,
            'start_time': start_time,
            'was_running': was_running,
            'archive_path': archive_path,
            'archive_size_bytes': archive_size,
            'duration_seconds': duration,
            'error': error,
            'named_volumes': named_volumes  # List of volume names or None
        }
        try:
            updates = getattr(self, 'stack_image_updates', {}) or {}
            if stack_name in updates:
                metric['images_pulled'] = True
                metric['pull_output'] = updates[stack_name].get('pull_output')
        except Exception:
            pass
        return metric
    
def _save_stack_metrics(self, stack_metrics):
        """Save stack metrics to database."""
        with get_db() as conn:
            cur = conn.cursor()
            for metric in stack_metrics:
                cur.execute("""
                    INSERT INTO job_stack_metrics (
                        job_id, stack_name, status, start_time, end_time,
                        duration_seconds, archive_path, archive_size_bytes,
                        was_running, log, error
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    self.job_id,
                    metric['stack_name'],
                    metric['status'],
                    metric['start_time'],
                    metric['start_time'] + __import__('datetime').timedelta(seconds=metric['duration_seconds']),
                    metric['duration_seconds'],
                    metric.get('archive_path'),
                    metric['archive_size_bytes'],
                    metric.get('was_running'),
                    '',  # Individual stack logs could be extracted if needed
                    metric.get('error')
                ))
            conn.commit()

        # Emit metrics event for listeners (best-effort)
        try:
            send_event(self.job_id, 'metrics', stack_metrics)
        except Exception:
            pass
    
def _update_job_status(self, status, end_time=None, duration=None, total_size=None, error=None):
        """Update job status in database."""
        log_text = '\n'.join(self.log_buffer)
        # Ensure the persisted log ends with a newline so subsequent incremental
        # appends do not concatenate with the final lines.
        if log_text and not log_text.endswith('\n'):
            log_text = log_text + '\n'
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE jobs SET 
                    status = %s, 
                    end_time = %s, 
                    duration_seconds = %s,
                    total_size_bytes = %s,
                    error = %s,
                    log = %s
                WHERE id = %s;
            """, (status, end_time, duration, total_size, error, log_text, self.job_id))
            conn.commit()

        # Emit status/job metadata update for live clients
        try:
            job_meta = {
                'id': self.job_id,
                'status': status,
                'start_time': None,
                'end_time': end_time,
                'duration_seconds': duration,
                'total_size_bytes': total_size,
                'reclaimed_bytes': None,
            }
            send_event(self.job_id, 'status', job_meta)
            try:
                # Also emit a global summary so the dashboard can update without polling
                send_global_event('job', job_meta)
                try:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.info("[SSE] Global event SENT (job status) id=%s status=%s end_time=%s duration=%s total_size=%s", self.job_id, status, end_time, duration, total_size)
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            pass
    
def _send_notification(self, stack_metrics, duration, total_size):
        """Send notification (SMTP via app settings)."""
        try:
            try:
                logger.info("Notifications: invoking send_archive_notification for archive=%s job=%s", self.config.get('name'), self.job_id)
            except Exception:
                pass
            send_archive_notification(self.config, self.job_id, stack_metrics, duration, total_size)
        except Exception as e:
            self.log('WARNING', f"Failed to send notification: {e}")

# Bind module-level implementations to ArchiveExecutor class so instance methods resolve correctly
ArchiveExecutor._create_job_record = _create_job_record_impl
ArchiveExecutor._phase_0_init = _phase_0_init
ArchiveExecutor._phase_1_process_stacks = _phase_1_process_stacks
ArchiveExecutor._process_single_stack = _process_single_stack
ArchiveExecutor._find_stack_path = _find_stack_path
ArchiveExecutor._is_stack_running = _is_stack_running
ArchiveExecutor._stop_stack = _stop_stack
ArchiveExecutor._start_stack = _start_stack
ArchiveExecutor._create_archive = _create_archive
ArchiveExecutor._should_run_retention = _should_run_retention
ArchiveExecutor._phase_2_retention = _phase_2_retention
ArchiveExecutor._phase_3_finalize = _phase_3_finalize
ArchiveExecutor._log_disk_usage = _log_disk_usage
ArchiveExecutor._create_stack_metric = _create_stack_metric
ArchiveExecutor._save_stack_metrics = _save_stack_metrics
ArchiveExecutor._update_job_status = _update_job_status
ArchiveExecutor._send_notification = _send_notification
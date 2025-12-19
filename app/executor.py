"""
Archive execution engine with phased processing.
"""
import os
import subprocess
import time
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from app.db import get_db
from app import utils
from app.stacks import validate_stack, find_compose_file
from app import utils


ARCHIVE_BASE = '/archives'


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
    
    def log(self, level, message):
        """Add log entry with timestamp."""
        timestamp = utils.local_now().strftime('%Y-%m-%d %H:%M:%S')
        prefix = "[SIMULATION] " if self.is_dry_run else ""
        log_line = f"[{timestamp}] [{level}] {prefix}{message}"
        self.log_buffer.append(log_line)
        print(log_line)
    
    def run(self, triggered_by='manual'):
        """Execute archive job with all phases."""
        start_time = utils.now()
        self.log('INFO', f"Starting archive job for: {self.config['name']}")
        
        # Create job record
        self.job_id = self._create_job_record(start_time, triggered_by)
        
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
    
    def _create_job_record(self, start_time, triggered_by):
        """Create initial job record in database."""
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
        if stop_containers and was_running:
            if not self._start_stack(stack_name, compose_path):
                self.log('ERROR', f"Failed to restart stack: {stack_name}")
                # Don't fail the whole job, archive was created successfully
        
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
        self.log('INFO', f"Stopping stack in {compose_path.parent}...")
        
        # Use 'down' without --volumes to cleanly stop and remove containers while preserving volumes
        cmd_parts = ['docker', 'compose', '-f', str(compose_path), 'down']
        self.log('INFO', f"Starting command: Stopping {stack_name} (docker compose down)")
        
        if self.is_dry_run:
            self.log('INFO', f"Would execute: docker compose -f {compose_path} down")
            return True
        
        try:
            result = subprocess.run(
                cmd_parts, capture_output=True, text=True, timeout=120, cwd=str(compose_path.parent)
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
        self.log('INFO', f"Starting stack in {compose_path.parent}...")
        
        cmd_parts = ['docker', 'compose', '-f', str(compose_path), 'up', '-d']
        self.log('INFO', f"Starting command: Starting {stack_name} (docker compose up -d)")
        
        if self.is_dry_run:
            self.log('INFO', f"Would execute: docker compose -f {compose_path} up -d")
            return True
        
        try:
            result = subprocess.run(
                cmd_parts, capture_output=True, text=True, timeout=120, cwd=str(compose_path.parent)
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
        
        # Create output path
        output_dir = Path(ARCHIVE_BASE) / archive_name / stack_name
        
        if ext:
            output_file = output_dir / f"{stack_name}_{timestamp}.{ext}"
        else:
            output_file = output_dir / f"{stack_name}_{timestamp}"
        
        # Skip archive creation if disabled in dry run
        if self.is_dry_run and not self.dry_run_config.get('create_archive', True):
            self.log('INFO', f"Skipping archive creation for '{stack_name}' (dry run disabled)")
            return str(output_file), 0
        
        if not self.is_dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
        
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
            reclaimed_bytes = run_retention(
                self.config, 
                self.job_id, 
                is_dry_run=self.is_dry_run,
                log_callback=self.log
            )
            
            # Update job with reclaimed bytes
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE jobs SET reclaimed_bytes = %s WHERE id = %s;",
                    (reclaimed_bytes, self.job_id)
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
        
        # Update job record
        self._update_job_status('success', end_time=end_time, duration=duration, total_size=total_size)
        
        # Save stack metrics
        self._save_stack_metrics(stack_metrics)
        
        # Send notification
        if not self.is_dry_run:
            self._send_notification(stack_metrics, duration, total_size)
        else:
            self.log('INFO', 'Would send notification (dry run)')
        
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
        return {
            'stack_name': stack_name,
            'status': status,
            'start_time': start_time,
            'was_running': was_running,
            'archive_path': archive_path,
            'archive_size_bytes': archive_size,
            'duration_seconds': duration,
            'error': error
        }
    
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
    
    def _update_job_status(self, status, end_time=None, duration=None, total_size=None, error=None):
        """Update job status in database."""
        log_text = '\n'.join(self.log_buffer)
        
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
    
    def _send_notification(self, stack_metrics, duration, total_size):
        """Send notification via Apprise."""
        try:
            from app.notifications import send_archive_notification
            send_archive_notification(self.config, self.job_id, stack_metrics, duration, total_size)
        except Exception as e:
            self.log('WARNING', f"Failed to send notification: {e}")

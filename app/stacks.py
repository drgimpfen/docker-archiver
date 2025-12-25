"""
Stack discovery and validation.
"""
import os
import glob
import subprocess
import json
from pathlib import Path
from app.utils import setup_logging, get_logger

# Configure logging using centralized setup
setup_logging()
logger = get_logger(__name__)


# Helper: centralized rule whether a destination should be ignored for stack discovery/mismatch warnings
def _is_ignored_destination(dst: str, src: str | None = None) -> bool:
    """Return True if the container destination should be ignored for discovery or mismatch warnings.

    Rules consolidated here:
      - ignore common system paths (/var, /etc, /usr, /proc, /sys)
      - ignore archive and docker socket destinations used by the app
      - ignore root
      - ignore overlay mounts
    """
    if not dst:
        return True
    try:
        dst_norm = str(Path(dst))
    except Exception:
        dst_norm = dst

    # system dirs
    if dst_norm.startswith('/var/') or dst_norm.startswith('/etc/') or dst_norm.startswith('/usr/'):
        return True
    if dst_norm.startswith('/proc/') or dst_norm.startswith('/sys/'):
        return True

    # fixed ignores (system/archive/docker-socket and ephemeral app dirs)
    if dst_norm in ('/archives', '/var/run/docker.sock', '/', '/tmp/downloads'):
        return True

    # src overlay (caller is expected to pass src) - overlay means not a host bind
    if src and src == 'overlay':
        return True

    return False


def get_own_container_mounts():
    """
    Get bind mount paths from our own container.
    Returns list of container paths that are bind-mounted (not named volumes).
    These are potential stack directories.
    """
    try:
        # Method 1: Try to get container ID and inspect
        container_id = None
        
        # Get our own container ID from /proc/self/cgroup
        try:
            with open('/proc/self/cgroup', 'r') as f:
                for line in f:
                    if 'docker' in line or 'containerd' in line:
                        # Extract container ID from cgroup path
                        parts = line.strip().split('/')
                        for part in reversed(parts):
                            if len(part) >= 12:  # Docker container IDs are at least 12 chars
                                container_id = part
                                break
                        break
        except (FileNotFoundError, OSError):
            pass
        
        if not container_id:
            # Fallback: try to get from hostname
            try:
                with open('/proc/sys/kernel/hostname', 'r') as f:
                    hostname = f.read().strip()
                    if len(hostname) >= 12:
                        container_id = hostname
            except (FileNotFoundError, OSError):
                pass
        
        if container_id:
            # Inspect our own container and print debug info
            try:
                result = subprocess.run(
                    ['docker', 'inspect', container_id],
                    capture_output=True, text=True, timeout=10
                )

                if result.returncode == 0:
                    try:
                        inspect_data = json.loads(result.stdout)
                    except Exception as e:
                        inspect_data = None

                    if inspect_data:
                        container_data = inspect_data[0]
                        mounts = container_data.get('Mounts', [])
                        bind_mounts = []
                        for mount in mounts:
                            mount_type = mount.get('Type', '')
                            if mount_type == 'bind':
                                destination = mount.get('Destination', '')
                                source = mount.get('Source', '')
                                # Skip system mounts and our own archives mount
                                if destination and not _is_ignored_destination(destination, source):
                                    bind_mounts.append(destination)
                        if bind_mounts:
                            return bind_mounts
            except Exception:
                # docker inspect failed or not available; fallback to mountinfo
                pass
        
        # Method 2: Fallback to /proc/self/mountinfo
        # This works even when docker inspect fails
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
                
                # Filter for bind mounts (source is not empty and not system paths)
                bind_mounts = []
                for mount_point, source in mounts:
                    if source and not _is_ignored_destination(mount_point, source):
                        bind_mounts.append(mount_point)

                if bind_mounts:
                    return bind_mounts
        except (FileNotFoundError, OSError, ValueError):
            pass
        
        return []
        
    except Exception as e:
        # Silently fail - we'll use default paths
        return []


def get_bind_mounts():
    """
    Return a list of bind mounts as dicts: { 'destination': '<path in container>', 'source': '<host source>' }.
    Tries docker inspect first, falls back to parsing /proc/self/mountinfo.
    """
    binds = []
    try:
        # Try docker inspect method (get container id then inspect)
        container_id = None
        try:
            with open('/proc/self/cgroup', 'r') as f:
                for line in f:
                    if 'docker' in line or 'containerd' in line:
                        parts = line.strip().split('/')
                        for part in reversed(parts):
                            if len(part) >= 12:
                                container_id = part
                                break
                        break
        except Exception:
            pass

        if not container_id:
            try:
                with open('/proc/sys/kernel/hostname', 'r') as f:
                    hostname = f.read().strip()
                    if len(hostname) >= 12:
                        container_id = hostname
            except Exception:
                pass

        if container_id:
            try:
                result = subprocess.run(['docker', 'inspect', container_id], capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    inspect_data = json.loads(result.stdout) if result.stdout else None
                    if inspect_data:
                        mounts = inspect_data[0].get('Mounts', [])
                        for m in mounts:
                            if m.get('Type') == 'bind':
                                dest = m.get('Destination')
                                src = m.get('Source')
                                if dest and src:
                                    # Skip system paths similar to get_own_container_mounts
                                    if (not dest.startswith('/var/') and not dest.startswith('/etc/') and not dest.startswith('/usr/') and
                                            not dest.startswith('/proc/') and not dest.startswith('/sys/') and dest != '/archives' and dest != '/var/run/docker.sock'):
                                        binds.append({'destination': dest, 'source': src})
                        return binds
            except Exception:
                pass

        # Fallback: parse /proc/self/mountinfo
        try:
            with open('/proc/self/mountinfo', 'r') as f:
                mounts = []
                for line in f:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    fs_type = parts[8] if len(parts) > 8 else ''
                    if fs_type in ['overlay', 'tmpfs', 'proc', 'sysfs', 'devpts', 'devtmpfs', 'cgroup', 'cgroup2']:
                        continue
                    mount_point = parts[4]
                    separator_idx = parts.index('-') if '-' in parts else -1
                    if separator_idx > 0 and len(parts) > separator_idx + 2:
                        source = parts[separator_idx + 2]
                        mounts.append((mount_point, source))

                # Consolidate destination filtering into helper to keep rules in one place
                for mount_point, source in mounts:
                    if source and not _is_ignored_destination(mount_point, source):
                        binds.append({'destination': mount_point, 'source': source})
                return binds
        except Exception:
            pass

    except Exception:
        pass

    return binds




def detect_bind_mismatches():
    """Return list of warning messages for binds where host/source != container/destination.

    Certain container destinations are intentionally ignored (e.g., `/tmp/downloads`) because
    they are used for ephemeral or intentionally host-bound storage that does not affect stack discovery.
    """
    warnings = []
    try:
        binds = get_bind_mounts()
        for b in binds:
            src = str(b.get('source') or '')
            dst = str(b.get('destination') or '')
            if not src or not dst:
                continue
            # Normalize paths
            try:
                src_norm = str(Path(src))
                dst_norm = str(Path(dst))
            except Exception:
                src_norm = src
                dst_norm = dst

            # Ignore well-known destinations which should not generate warnings
            if _is_ignored_destination(dst_norm, src_norm):
                continue

            if src_norm != dst_norm:
                # Return a concise message with an actionable hint
                warnings.append(
                    f"Host path '{src_norm}' is mounted as container path '{dst_norm}'. Host and container paths must be identical; bind mounts are mandatory and mismatched mounts will be ignored for discovery."
                )
    except Exception:
        pass

    return warnings


def get_mismatched_destinations():
    """Return a list of container destination paths where bind source != destination."""
    mismatches = []
    try:
        binds = get_bind_mounts()
        for b in binds:
            src = str(b.get('source') or '')
            dst = str(b.get('destination') or '')
            if not src or not dst:
                continue
            try:
                src_norm = str(Path(src))
                dst_norm = str(Path(dst))
            except Exception:
                src_norm = src
                dst_norm = dst

            # Ignore well-known destinations that should not be treated as mismatches
            if _is_ignored_destination(dst_norm, src_norm):
                continue

            if src_norm != dst_norm and dst_norm not in mismatches:
                mismatches.append(dst_norm)
    except Exception:
        pass
    return mismatches


def get_stack_mount_paths():
    """
    Get container paths where stacks should be searched.
    Automatically detected from our own container's bind mounts.
    Returns list of container paths to search in.
    """
    # Auto-detect from our own container mounts
    auto_detected = get_own_container_mounts()
    if auto_detected:
        return auto_detected
    
    # Final fallback: default path
    return ["/opt/stacks"]


def discover_stacks():
    """
    Discover stacks from configured mount directories.
    Searches max 1 level deep for compose.y(a)ml or docker-compose.y(a)ml files.
    Returns list of dicts with stack info: {name, path, compose_file, mount_source}
    """
    stacks = []
    
    # Get configured mount paths
    mount_paths = get_stack_mount_paths()
    
    # Determine destinations to ignore (mismatched bind destinations)
    ignore_dests = set(get_mismatched_destinations())

    for mount_base in mount_paths:
        # Skip any mount that is an ignored destination or is under one
        skip_mount = False
        for ignored in ignore_dests:
            if str(mount_base) == str(ignored) or str(mount_base).startswith(str(ignored) + '/'):
                skip_mount = True
                break
        if skip_mount:
            # Do not scan this mount path at all
            continue

        mount_path = Path(mount_base)
        if not mount_path.exists():
            continue
        
        mount_name = mount_path.name
        
        # Check if mount_path itself contains a compose file (direct stack mount)
        compose_file = find_compose_file(mount_path)
        if compose_file:
            # Skip our own application stack (guards against self-backup)
            try:
                if _is_local_app_stack(mount_path):
                    logger.debug("Skipping local app stack at %s", mount_path)
                    # don't add to discovered stacks
                    compose_file = None
            except Exception:
                pass

        if compose_file:
            stacks.append({
                'name': mount_name,
                'path': str(mount_path),
                'compose_file': compose_file,
                'mount_source': mount_name
            })
        else:
            # Search one level deeper for stacks
            try:
                for stack_dir in mount_path.iterdir():
                    if not stack_dir.is_dir():
                        continue
                    
                    compose_file = find_compose_file(stack_dir)
                    if compose_file:
                        # Skip our own application stack (guards against self-backup)
                        try:
                            if _is_local_app_stack(stack_dir):
                                logger.debug("Skipping local app stack at %s", stack_dir)
                                continue
                        except Exception:
                            pass

                        stacks.append({
                            'name': stack_dir.name,
                            'path': str(stack_dir),
                            'compose_file': compose_file,
                            'mount_source': mount_name
                        })
            except (OSError, PermissionError):
                # Skip directories we can't read
                continue

    return sorted(stacks, key=lambda x: x['name'])


def get_visible_stacks():
    """Return discovered stacks excluding the local application stack (for UI selection).

    This ensures the app's own stack is not shown in create/edit modals and cannot be
    selected by users for backups (prevents accidental self-backups/self-shutdown).
    """
    try:
        stacks = discover_stacks()
        visible = []
        for s in stacks:
            try:
                if _is_local_app_stack(s['path']):
                    continue
            except Exception:
                # If detection fails, conservatively include the stack
                visible.append(s)
                continue
            visible.append(s)
        return visible
    except Exception:
        return discover_stacks()


def find_compose_file(directory):
    """
    Find compose file in directory.
    Looks for: compose.yml, compose.yaml, docker-compose.yml, docker-compose.yaml
    Returns filename if found, None otherwise.
    """
    compose_files = [
        'compose.yml',
        'compose.yaml',
        'docker-compose.yml',
        'docker-compose.yaml'
    ]
    
    for filename in compose_files:
        filepath = Path(directory) / filename
        if filepath.is_file():
            return filename


def _is_local_app_stack(path):
    """Return True if the compose file at `path` appears to be the application itself.

    Heuristics used:
    - service named 'app' or 'archiver' under `services:`
    - image name containing 'archiver' (case-insensitive)
    - container_name containing 'app' or 'archiver'

    This is a best-effort check to prevent selecting the running application stack
    for backup (which could stop the app and cause self-termination).
    """
    try:
        import re
        from pathlib import Path
        comp = find_compose_file(Path(path))
        if not comp:
            return False
        cf = Path(path) / comp
        txt = cf.read_text(encoding='utf-8', errors='ignore')

        # Look for service names under 'services:'
        if re.search(r'^\s*services\s*:\s*$', txt, re.M):
            servs = re.findall(r'^\s{2,}([A-Za-z0-9_\-]+)\s*:', txt, re.M)
            for s in servs:
                if s.lower() in ('app', 'archiver'):
                    return True

        # Check for image referencing the archiver
        if re.search(r'image\s*:\s*.*archiver', txt, re.I):
            return True

        # Check for container_name that looks like app or archiver
        if re.search(r'container_name\s*:\s*(app|archiver)', txt, re.I):
            return True

    except Exception:
        pass
    return False
    
    return None


def validate_stack(stack_path):
    """
    Validate that a stack directory exists and contains a compose file.
    Returns (valid: bool, error_message: str)
    """
    path = Path(stack_path)
    
    if not path.exists():
        return False, f"Stack directory does not exist: {stack_path}"
    
    if not path.is_dir():
        return False, f"Stack path is not a directory: {stack_path}"
    
    compose_file = find_compose_file(path)
    if not compose_file:
        return False, f"No compose file found in {stack_path}"
    
    return True, None


def get_stack_info(stack_path):
    """Get detailed info about a stack."""
    path = Path(stack_path)
    compose_file = find_compose_file(path)
    
    return {
        'name': path.name,
        'path': str(path),
        'compose_file': compose_file,
        'valid': compose_file is not None
    }

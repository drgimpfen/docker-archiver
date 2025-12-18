"""
Stack discovery and validation.
"""
import os
import glob
from pathlib import Path


LOCAL_MOUNT_BASE = '/local'


def discover_stacks():
    """
    Discover stacks from /local/* directories.
    Searches max 1 level deep for compose.y(a)ml or docker-compose.y(a)ml files.
    Returns list of dicts with stack info: {name, path, compose_file, mount_source}
    """
    stacks = []
    
    if not os.path.exists(LOCAL_MOUNT_BASE):
        return stacks
    
    # Iterate over mounted directories in /local
    for mount_dir in Path(LOCAL_MOUNT_BASE).iterdir():
        if not mount_dir.is_dir():
            continue
        
        mount_name = mount_dir.name
        
        # Check if mount_dir itself contains a compose file (direct stack mount)
        compose_file = find_compose_file(mount_dir)
        if compose_file:
            stacks.append({
                'name': mount_name,
                'path': str(mount_dir),
                'compose_file': compose_file,
                'mount_source': mount_name
            })
        else:
            # Search one level deeper for stacks
            for stack_dir in mount_dir.iterdir():
                if not stack_dir.is_dir():
                    continue
                
                compose_file = find_compose_file(stack_dir)
                if compose_file:
                    stacks.append({
                        'name': stack_dir.name,
                        'path': str(stack_dir),
                        'compose_file': compose_file,
                        'mount_source': mount_name
                    })
    
    return sorted(stacks, key=lambda x: x['name'])


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

"""
Main Flask application with Blueprints.
"""
import os
import threading

__version__ = '0.7.0'
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from app.db import init_db, get_db
from app.auth import login_required, authenticate_user, create_user, get_user_count, get_current_user
from app.scheduler import init_scheduler, get_next_run_time
from app.stacks import discover_stacks, get_stack_mount_paths
from app.downloads import get_download_by_token, prepare_archive_for_download
from app.notifications import get_setting
from app.utils import format_bytes, format_duration, get_disk_usage
from pathlib import Path

# Import blueprints
from app.routes import archives, history, settings, profile, api


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')


@app.context_processor
def inject_app_version():
    """Inject application version into all templates as `app_version`."""
    try:
        return {'app_version': __version__}
    except Exception:
        return {'app_version': 'unknown'}

# Security: CSRF Protection
csrf = CSRFProtect(app)
# Exempt download endpoint (uses tokens) and health check
csrf.exempt('download_archive')
csrf.exempt('health')

# Security: Rate Limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# Security Headers
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; font-src 'self' https://cdn.jsdelivr.net"
    return response

# Register blueprints
app.register_blueprint(archives.bp)
app.register_blueprint(history.bp)
app.register_blueprint(settings.bp)
app.register_blueprint(profile.bp)
app.register_blueprint(api.bp)

# Exempt API blueprint from CSRF (uses Bearer tokens)
csrf.exempt(api.bp)

# Run startup discovery once, safely, on first request using a guarded before_request handler.
startup_discovery_done = False
startup_discovery_lock = threading.Lock()

def run_startup_discovery():
    """Run stack/mount detection once per process (guarded by a lock)."""
    global startup_discovery_done
    if startup_discovery_done:
        return
    with startup_discovery_lock:
        if startup_discovery_done:
            return
        try:
            mount_paths = get_stack_mount_paths()
            print(f"[DEBUG] Auto-detected mount paths: {mount_paths}")
            stacks = discover_stacks()
            print(f"[INFO] Discovered {len(stacks)} stacks:")
            for s in stacks:
                print(f"  - {s['name']} (at {s['path']}, compose: {s.get('compose_file')})")

            # Detect bind mount mismatches and persist warnings to app config for UI
            try:
                from app.stacks import detect_bind_mismatches, get_mismatched_destinations
                bind_warnings = detect_bind_mismatches()
                # Deduplicate warnings while preserving order
                bind_warnings = list(dict.fromkeys(bind_warnings)) if bind_warnings else []
                if bind_warnings:
                    for w in bind_warnings:
                        # Ensure multiline warnings are clearly prefixed in logs
                        for line in str(w).splitlines():
                            print(f"[WARNING] {line}")
                app.config['BIND_MISMATCH_WARNINGS'] = bind_warnings
                # Also persist the exact container destinations that are mismatched so we can ignore them
                ignored = list(dict.fromkeys(get_mismatched_destinations()))
                app.config['IGNORED_BIND_DESTINATIONS'] = ignored
                if ignored:
                    print(f"[INFO] Ignoring stacks under destinations: {ignored}")
            except Exception as e:
                print(f"[DEBUG] Could not detect bind mismatches: {e}")

        except Exception as e:
            print(f"[ERROR] Startup mount/stack detection failed: {e}")
        finally:
            startup_discovery_done = True


@app.before_request
def _ensure_startup_discovery():
    """Ensure the startup discovery has run (runs only once per process)."""
    run_startup_discovery()

@app.context_processor
def inject_bind_warnings():
    """Inject bind mount mismatch warnings and ignored destinations into templates."""
    try:
        return {
            'bind_warnings': app.config.get('BIND_MISMATCH_WARNINGS', []),
            'ignored_bind_destinations': app.config.get('IGNORED_BIND_DESTINATIONS', [])
        }
    except Exception:
        return {'bind_warnings': [], 'ignored_bind_destinations': []}

# Initialize scheduler on startup
init_scheduler()


# Custom Jinja2 filters
@app.template_filter('stack_color')
def stack_color_filter(stack_name):
    """Generate a deterministic color for a stack name with good readability."""
    import hashlib
    
    # Generate hash from stack name
    hash_value = int(hashlib.md5(stack_name.encode()).hexdigest()[:8], 16)
    
    # Generate hue (0-360) from hash
    hue = hash_value % 360
    
    # Use fixed saturation and lightness for good readability
    # Saturation: 65% (not too dull, not too vibrant)
    # Lightness: 45% (dark enough for white text)
    return f"hsl({hue}, 65%, 45%)"


@app.template_filter('datetime')
def datetime_filter(dt, format_string='%Y-%m-%d %H:%M:%S'):
    """Format datetime in local timezone."""
    from app.utils import format_datetime
    return format_datetime(dt, format_string)


# Core routes
@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok'}), 200


@app.route('/')
@login_required
def index():
    """Dashboard page."""
    from datetime import datetime, timedelta
    
    # Check maintenance mode
    maintenance_mode = get_setting('maintenance_mode', 'false').lower() == 'true'
    
    # Get disk usage
    disk = get_disk_usage()
    
    # Get all archives
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM archives ORDER BY name;")
        archives_list = cur.fetchall()
        
        # Dashboard statistics
        total_archives_configured = len(archives_list)
        active_schedules = sum(1 for a in archives_list if a['schedule_enabled'])
        
        # Last 24h jobs
        cur.execute("""
            SELECT COUNT(*) as count FROM jobs 
            WHERE job_type = 'archive' 
            AND status = 'success' 
            AND start_time >= NOW() - INTERVAL '24 hours';
        """)
        jobs_last_24h = cur.fetchone()['count']
        
        # Next scheduled job
        next_scheduled = None
        for archive in archives_list:
            if archive['schedule_enabled']:
                next_run = get_next_run_time(archive['id'])
                if next_run and (not next_scheduled or next_run < next_scheduled):
                    next_scheduled = next_run
        
        # Total archives size
        cur.execute("""
            SELECT SUM(total_size_bytes) as total FROM jobs 
            WHERE job_type = 'archive' AND status = 'success';
        """)
        result = cur.fetchone()
        total_archives_size = result['total'] if result and result['total'] else 0
        
        # Enrich archives with stats
        archive_list = []
        for archive in archives_list:
            archive_dict = dict(archive)
            
            # Get job count
            cur.execute(
                "SELECT COUNT(*) as count FROM jobs WHERE archive_id = %s AND job_type = 'archive';",
                (archive['id'],)
            )
            archive_dict['job_count'] = cur.fetchone()['count']
            
            # Get last job
            cur.execute("""
                SELECT start_time, status FROM jobs 
                WHERE archive_id = %s AND job_type = 'archive'
                ORDER BY start_time DESC LIMIT 1;
            """, (archive['id'],))
            last_job = cur.fetchone()
            archive_dict['last_run'] = last_job['start_time'] if last_job else None
            archive_dict['last_status'] = last_job['status'] if last_job else None
            
            # Get next run time
            if archive['schedule_enabled']:
                archive_dict['next_run'] = get_next_run_time(archive['id'])
            else:
                archive_dict['next_run'] = None
            
            # Get total size
            cur.execute("""
                SELECT SUM(total_size_bytes) as total FROM jobs 
                WHERE archive_id = %s AND job_type = 'archive' AND status = 'success';
            """, (archive['id'],))
            result = cur.fetchone()
            archive_dict['total_size'] = result['total'] if result and result['total'] else 0
            
            archive_list.append(archive_dict)
        
        # Get recent jobs (last 10)
        cur.execute("""
            SELECT j.*, a.name as archive_name, a.stacks as archive_stacks,
                   (SELECT STRING_AGG(stack_name, ',') FROM job_stack_metrics WHERE job_id = j.id) as stack_names,
                   EXTRACT(EPOCH FROM (j.end_time - j.start_time))::integer as duration_seconds
            FROM jobs j
            LEFT JOIN archives a ON j.archive_id = a.id
            ORDER BY j.start_time DESC
            LIMIT 10;
        """)
        recent_jobs = cur.fetchall()
    
    # Calculate disk health status
    disk_percent = disk.get('percent', 0)
    if disk_percent >= 90:
        disk_status = 'danger'
    elif disk_percent >= 70:
        disk_status = 'warning'
    else:
        disk_status = 'success'
    
    return render_template(
        'index.html',
        archives=archive_list,
        recent_jobs=recent_jobs,
        disk=disk,
        disk_status=disk_status,
        total_archives_size=total_archives_size,
        total_archives_configured=total_archives_configured,
        active_schedules=active_schedules,
        jobs_last_24h=jobs_last_24h,
        next_scheduled=next_scheduled,
        maintenance_mode=maintenance_mode,
        format_bytes=format_bytes,
        format_duration=format_duration,
        current_user=get_current_user()
    )


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    """Login page."""
    # Check if initial setup is needed
    if get_user_count() == 0:
        return redirect(url_for('setup'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = authenticate_user(username, password)
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password', 'danger')
    
    return render_template('login.html')


@app.route('/logout')
def logout():
    """Logout."""
    session.clear()
    flash('Logged out successfully', 'info')
    return redirect(url_for('login'))


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    """Initial setup page (create first admin user)."""
    # Redirect if users already exist
    if get_user_count() > 0:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        email = request.form.get('email')
        
        if not username or not password:
            flash('Username and password are required', 'danger')
        else:
            try:
                user_id = create_user(username, password, email, role='admin')
                flash('Admin user created successfully! Please log in.', 'success')
                return redirect(url_for('login'))
            except Exception as e:
                flash(f'Error creating user: {e}', 'danger')
    
    return render_template('setup.html')


@app.route('/api/stacks')
@login_required
def api_get_stacks():
    """API endpoint to get available stacks."""
    stacks = discover_stacks()
    return jsonify({'stacks': stacks})


@app.route('/download/<token>')
def download_archive(token):
    """Download archive by token (no auth required)."""
    from app.security import is_safe_path
    
    download_info = get_download_by_token(token)
    
    if not download_info:
        return "Invalid or expired download link", 404
    
    archive_path = download_info['archive_path']
    
    # Security: Validate path is within archives directory
    if not is_safe_path('/archives', archive_path):
        return "Invalid archive path", 403
    
    # Prepare archive (create if folder)
    actual_path, should_cleanup = prepare_archive_for_download(archive_path, output_format='tar.gz')
    
    if not actual_path:
        return "Archive file not found or could not be created", 404
    
    # Send file
    try:
        return send_file(
            actual_path,
            as_attachment=True,
            download_name=os.path.basename(actual_path)
        )
    except Exception as e:
        return f"Error downloading file: {e}", 500


if __name__ == '__main__':
    # Initialize database
    init_db()
    
    # Run development server
    app.run(host='0.0.0.0', port=8080, debug=True)

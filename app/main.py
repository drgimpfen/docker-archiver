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
from app.utils import format_bytes, format_duration, get_disk_usage, to_iso_z
from pathlib import Path

# Import blueprints
from app.routes import archives, history, settings, profile, api, dashboard


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
app.register_blueprint(dashboard.bp)

# Exempt API blueprint from CSRF (uses Bearer tokens)
csrf.exempt(api.bp)

# Run startup discovery once, safely, on first request using a guarded before_request handler.
startup_discovery_done = False
startup_discovery_lock = threading.Lock()

def run_startup_discovery():
    """Run stack/mount detection once per process (guarded by a lock).

    To avoid noisy duplicate logs across multiple worker processes, we write a
    container-local sentinel file to indicate that discovery logging has already
    been performed; discovery still runs per-process but only the first process
    emits the verbose logs.
    """
    global startup_discovery_done
    if startup_discovery_done:
        return
    with startup_discovery_lock:
        if startup_discovery_done:
            return

        # Determine whether this process should emit verbose logs
        verbose = False
        sentinel_log = '/tmp/da_startup_discovery_logged'
        try:
            fd = os.open(sentinel_log, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            verbose = True
        except FileExistsError:
            verbose = False
        except Exception:
            verbose = True

        try:
            mount_paths = get_stack_mount_paths()
            if verbose:
                print(f"[DEBUG] Auto-detected mount paths: {mount_paths}")
            stacks = discover_stacks()
            if verbose:
                print(f"[INFO] Discovered {len(stacks)} stacks:")
                for s in stacks:
                    print(f"  - {s['name']} (at {s['path']}, compose: {s.get('compose_file')})")

            # Detect bind mount mismatches and persist warnings to app config for UI
            try:
                from app.stacks import detect_bind_mismatches, get_mismatched_destinations
                bind_warnings = detect_bind_mismatches()
                # Deduplicate warnings while preserving order
                bind_warnings = list(dict.fromkeys(bind_warnings)) if bind_warnings else []
                app.config['BIND_MISMATCH_WARNINGS'] = bind_warnings
                # Also persist the exact container destinations that are mismatched so we can ignore them
                ignored = list(dict.fromkeys(get_mismatched_destinations()))
                app.config['IGNORED_BIND_DESTINATIONS'] = ignored
                if verbose and bind_warnings:
                    for w in bind_warnings:
                        # Ensure multiline warnings are clearly prefixed in logs
                        for line in str(w).splitlines():
                            print(f"[WARNING] {line}")
                if verbose and ignored:
                    print(f"[INFO] Ignoring stacks under destinations: {ignored}")
            except Exception as e:
                if verbose:
                    print(f"[DEBUG] Could not detect bind mismatches: {e}")

        except Exception as e:
            if verbose:
                print(f"[ERROR] Startup mount/stack detection failed: {e}")
        finally:
            # Start asynchronous cleanup of stale 'running' jobs to avoid UI confusion.
            try:
                # Use container-local sentinel so cleanup runs only once
                sentinel = '/tmp/da_startup_cleanup_started'
                created = False
                try:
                    fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode())
                    os.close(fd)
                    created = True
                except FileExistsError:
                    created = False
                except Exception:
                    created = True

                if not created:
                    if verbose:
                        print("[Startup] Skipping stale job cleanup (already started by another process)")
                else:
                    def _cleanup_stale():
                        try:
                            from app.db import mark_stale_running_jobs
                            # On startup mark any running jobs without end_time as failed to avoid
                            # stuck running states and UI confusion.
                            print(f"[Startup] Running stale job cleanup (marking running jobs without end_time as failed)")
                            marked = mark_stale_running_jobs(None)
                            if marked and int(marked) > 0:
                                print(f"[Startup] Marked {marked} running jobs as failed on startup")
                        except Exception as se:
                            print(f"[Startup] Stale job cleanup failed: {se}")
                    t = threading.Thread(target=_cleanup_stale, daemon=True)
                    t.start()
            except Exception as e:
                if verbose:
                    print(f"[Startup] Failed to start stale job cleanup thread: {e}")

            # Run downloads startup rescan to restore any missing download artifacts if possible
            try:
                from app.downloads import startup_rescan_downloads
                startup_rescan_downloads()
                if verbose:
                    print("[Startup] Download startup rescan complete")
            except Exception as e:
                if verbose:
                    print(f"[Startup] Download startup rescan failed: {e}")

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


@app.template_filter('iso_z')
def iso_z_filter(dt):
    """Convert a datetime-like object to ISO UTC with trailing 'Z'."""
    try:
        return to_iso_z(dt)
    except Exception:
        return dt

# Core routes
@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok'}), 200


# Dashboard route moved to `app.routes.dashboard` as a Blueprint. See `app/routes/dashboard.py` for implementation.


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
            return redirect(url_for('dashboard.index'))
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
    
    # First, fetch raw token row to determine specific failure reasons without
    # incrementing the download counter.
    token_row = get_download_token_row(token)
    if not token_row:
        return render_template('download_error.html', reason='Ungültiger Download-Link', hint='Der Link ist ungültig oder wurde nie erstellt.'), 404

    # Check expiry
    from datetime import datetime
    now = datetime.utcnow()
    expires_at = token_row.get('expires_at')
    if expires_at and expires_at <= now:
        return render_template('download_error.html', reason='Der Download-Link ist abgelaufen', hint='Bitte erstelle einen neuen Download-Link für die gewünschte Datei.'), 410

    # Check max downloads
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = 'max_token_downloads';")
        setting = cur.fetchone()
        max_downloads = int(setting['value']) if setting else 3

    if token_row.get('downloads', 0) >= max_downloads:
        return render_template('download_error.html', reason='Download-Limit erreicht', hint='Der Download wurde zu oft verwendet. Erstelle bitte einen neuen Download.'), 429

    archive_path = token_row['archive_path']
    # Security: Validate path is within archives directory
    if archive_path and not is_safe_path('/archives', archive_path):
        return render_template('download_error.html', reason='Ungültiger Pfad', hint='Der Pfad zum Archiv ist ungültig.'), 403

    # Prepare archive (create if folder)
    actual_path, should_cleanup = prepare_archive_for_download(archive_path, output_format='tar.gz')

    if not actual_path or not Path(actual_path).exists():
        return render_template('download_error.html', reason='Datei nicht gefunden', hint='Die Datei ist nicht mehr vorhanden. Du kannst den Download erneut generieren, indem du ein neues Archiv erstellst oder die Download‑Aktion nochmal startest.'), 404

    # Before sending, increment download counter
    increment_download_count(token)

    # Send file
    try:
        return send_file(
            actual_path,
            as_attachment=True,
            download_name=os.path.basename(actual_path)
        )
    except Exception as e:
        return render_template('download_error.html', reason='Fehler beim Herunterladen', hint=str(e)), 500


if __name__ == '__main__':
    # Initialize database
    init_db()
    
    # Run development server
    app.run(host='0.0.0.0', port=8080, debug=True)

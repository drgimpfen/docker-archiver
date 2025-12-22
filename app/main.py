"""
Main Flask application with Blueprints.
"""
import os
import threading
from app.utils import setup_logging, get_logger, get_sentinel_path, format_bytes, format_duration, get_disk_usage, to_iso_z, format_datetime
# Centralized logging setup (use only LOG_LEVEL env var)
setup_logging()
logger = get_logger(__name__)

__version__ = '0.7.0'
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from app.db import init_db, get_db
from app.auth import login_required, authenticate_user, create_user, get_user_count, get_current_user
from app.scheduler import init_scheduler, get_next_run_time
from app.stacks import discover_stacks, get_stack_mount_paths
from app.downloads import get_download_by_token, get_download_token_row, prepare_archive_for_download, increment_download_count, DOWNLOADS_PATH
from app.notifications import get_setting
import shutil
from app import utils
from pathlib import Path

# Import blueprints
# Core routes and blueprints
from app.routes import history, settings, profile, api, dashboard
# Import API-specific archives blueprint from new location
from app.routes.api import archives as api_archives
# Also import legacy archives module (contains legacy redirects)
from app.routes import archives_legacy as legacy_archives


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
    response.headers['Content-Security-Policy'] = "default-src 'self'; connect-src 'self' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; font-src 'self' https://cdn.jsdelivr.net"
    return response

# Register blueprints
# Register archives API blueprint (moved)
app.register_blueprint(api_archives.bp)
# Legacy routes for backward compatibility (redirects `/archives/*` → `/api/archives/*`)
app.register_blueprint(legacy_archives.legacy_bp)

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
        sentinel_log = get_sentinel_path('da_startup_discovery_logged')
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
                logger.debug("Auto-detected mount paths: %s", mount_paths)
            stacks = discover_stacks()
            if verbose:
                logger.info("Discovered %d stacks:", len(stacks))
                for s in stacks:
                    logger.info("  - %s (at %s, compose: %s)", s['name'], s['path'], s.get('compose_file'))

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
                            logger.warning("%s", line)
                if verbose and ignored:
                    logger.info("Ignoring stacks under destinations: %s", ignored)
            except Exception as e:
                    if verbose:
                        logger.debug("Could not detect bind mismatches: %s", e)
        except Exception as e:
            if verbose:
                logger.exception("Startup mount/stack detection failed: %s", e)
            else:
                logger.debug("Startup mount/stack detection failed: %s", e)
        finally:
            # Start asynchronous cleanup of stale 'running' jobs to avoid UI confusion.
            try:
                # Use container-local sentinel so cleanup runs only once
                sentinel = get_sentinel_path('da_startup_cleanup_started')
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
                        logger.info("[Startup] Skipping stale job cleanup (already started by another process)")
                else:
                    def _cleanup_stale():
                        try:
                            from app.db import mark_stale_running_jobs
                            # On startup mark any running jobs without end_time as failed to avoid
                            # stuck running states and UI confusion.
                            logger.info("[Startup] Running stale job cleanup (marking running jobs without end_time as failed)")
                            marked = mark_stale_running_jobs(None)
                            if marked and int(marked) > 0:
                                logger.info("[Startup] Marked %s running jobs as failed on startup", marked)
                        except Exception as se:
                            logger.exception("[Startup] Stale job cleanup failed: %s", se)
                    t = threading.Thread(target=_cleanup_stale, daemon=True)
                    t.start()
            except Exception as e:
                if verbose:
                    logger.exception("[Startup] Failed to start stale job cleanup thread: %s", e)

            # Run downloads startup rescan to restore any missing download artifacts if possible
            try:
                from app.downloads import startup_rescan_downloads
                startup_rescan_downloads()
                if verbose:
                    logger.info("[Startup] Download startup rescan complete")
            except Exception as e:
                if verbose:
                    logger.exception("[Startup] Download startup rescan failed: %s", e)

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

# Start a Redis listener in every process so hot-reload signals are received by
# whichever process is able to trigger schedule reloads (the scheduler owner).
try:
    from app.scheduler import start_redis_listener
    start_redis_listener()
except Exception as e:
    logger.exception("[Main] Could not start Redis listener: %s", e)


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
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    expires_at = token_row.get('expires_at')
    if expires_at:
        try:
            # Normalize to UTC-aware datetime for reliable comparison
            expires_at = expires_at.astimezone(timezone.utc) if getattr(expires_at, 'tzinfo', None) else expires_at.replace(tzinfo=timezone.utc)
        except Exception:
            try:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            except Exception:
                expires_at = None
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
    # We enforce that served downloads always come from DOWNLOADS_PATH.
    # If the token references a different path, attempt to regenerate a download
    # inside DOWNLOADS_PATH (copy file or create archive from folder) and update
    # the token to point to that generated file.
    from pathlib import Path as _Path
    def _is_under_downloads(p):
        try:
            return _Path(p).resolve().is_relative_to(DOWNLOADS_PATH.resolve())
        except Exception:
            # Fallback for Python <3.9: compare parts
            try:
                return str(_Path(p).resolve()).startswith(str(DOWNLOADS_PATH.resolve()))
            except Exception:
                return False

    actual_path = None
    should_cleanup = False

    if archive_path:
        try:
            p = _Path(archive_path)
            if p.exists() and _is_under_downloads(p):
                actual_path = str(p.resolve())
            else:
                # If token is already preparing, show preparing message
                if token_row.get('is_preparing'):
                    return render_template('download_error.html', reason='Download wird vorbereitet', hint='Der Download wird gerade neu erstellt. Bitte versuche es in ein paar Minuten erneut.'), 202

                # Try to detect if an original source exists (either the token's archive_path or a job metric)
                source_exists = False
                try:
                    if p.exists():
                        source_exists = True
                    else:
                        job_id = token_row.get('job_id')
                        stack_name = token_row.get('stack_name')
                        if job_id and stack_name:
                            with get_db() as conn:
                                cur = conn.cursor()
                                cur.execute("""
                                    SELECT archive_path FROM job_stack_metrics
                                    WHERE job_id = %s AND stack_name = %s AND archive_path IS NOT NULL
                                    ORDER BY start_time DESC LIMIT 1;
                                """, (job_id, stack_name))
                                row = cur.fetchone()
                                if row and row.get('archive_path') and _Path(row['archive_path']).exists():
                                    source_exists = True
                except Exception:
                    source_exists = False

                if source_exists:
                    # Start background regeneration which will set is_preparing=true
                    try:
                        from app.downloads import regenerate_token
                        threading.Thread(target=regenerate_token, args=(token,), daemon=True).start()
                        return render_template('download_error.html', reason='Download wird vorbereitet', hint='Der Download wird gerade neu erstellt. Bitte versuche es in ein paar Minuten erneut.'), 202
                    except Exception as e:
                        logger.exception("[Downloads] Failed to start background regeneration for token %s: %s", token, e)
                        # Fall back to reporting not found
                
        except Exception as e:
            logger.exception("[Downloads] Error while resolving archive_path for token %s: %s", token, e)

    if not actual_path or not _Path(actual_path).exists():
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

"""
Main Flask application with Blueprints.
"""
import os
import threading
from datetime import datetime
from app.utils import setup_logging, get_logger, get_sentinel_path, format_bytes, format_duration, get_disk_usage, to_iso_z, format_datetime
# Centralized logging setup (use only LOG_LEVEL env var)
setup_logging()
logger = get_logger(__name__)

__version__ = '0.8.0'
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from app.db import init_db, get_db
from app.auth import login_required, authenticate_user, create_user, get_user_count, get_current_user
from app.scheduler import init_scheduler, get_next_run_time
from app.stacks import discover_stacks, get_stack_mount_paths
from app.notifications.helpers import get_setting
import shutil
from app import utils
from pathlib import Path

# Import blueprints
# Core routes and blueprints
from app.routes import history, settings, api, dashboard, downloads
# Import API-specific archives blueprint from new location
from app.routes.api import archives as api_archives
# Legacy redirects removed — `/archives/*` legacy endpoints were deprecated and are no longer registered.


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
# Exempt health check
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
# Legacy redirects removed — `/archives/*` legacy endpoints were deprecated and are no longer registered.

app.register_blueprint(history.bp)
app.register_blueprint(downloads.bp)
app.register_blueprint(settings.bp)
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

            # Resume any pending download packing tasks on startup
            try:
                from app.routes.api.downloads import resume_pending_downloads
                generate_on_start = os.environ.get('DOWNLOADS_AUTO_GENERATE_ON_STARTUP', 'false').lower() in ('1','true','yes')
                t = threading.Thread(target=resume_pending_downloads, args=(generate_on_start,), daemon=True)
                t.start()
                if verbose:
                    logger.info('[Startup] Resuming pending download packing tasks (generate_missing=%s)', generate_on_start)
            except Exception as e:
                logger.exception('[Startup] Failed to resume pending downloads: %s', e)

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
def download_file(token):
    """Serve file download using secure token (publicly accessible)."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT file_path, archive_path, notify_emails, stack_name, expires_at, is_packing
                FROM download_tokens
                WHERE token = %s;
            """, (token,))
            token_data = cur.fetchone()

        if not token_data:
            return render_template('download_error.html', reason='Download token is invalid or does not exist.', hint='Please request a fresh download link.'), 404

        # Normalize expiry and compare with UTC-aware now
        expires = utils.ensure_utc(token_data.get('expires_at'))
        if not expires or expires < utils.now():
            return render_template('download_error.html', reason='This download link has expired.', hint='Request a new download link.'), 410

        if token_data['is_packing']:
            # Show preparing page and allow user to register for notification. Prevent simultaneous regenerate triggers.
            return render_template('download_preparing.html', token=token, stack_name=token_data.get('stack_name'), notify_emails=token_data.get('notify_emails'), expires_at=token_data.get('expires_at')), 202

        file_path = token_data.get('file_path')
        if not file_path or not Path(file_path).exists():
            # If the archive_path is already a file (created by previous runs), adopt it and serve
            archive_path = token_data.get('archive_path')
            if archive_path and Path(archive_path).exists() and Path(archive_path).is_file():
                try:
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("UPDATE download_tokens SET file_path = %s, is_packing = FALSE WHERE token = %s;", (archive_path, token))
                        conn.commit()
                    file_path = archive_path
                except Exception:
                    pass

            # If archive_path exists and is a directory, present regeneration form (public)
            if archive_path and Path(archive_path).exists() and Path(archive_path).is_dir():
                return render_template('download_missing.html', token=token, stack_name=token_data.get('stack_name'), notify_emails=token_data.get('notify_emails'), expires_at=token_data.get('expires_at'))

            # Optionally start regeneration on access if enabled
            if os.environ.get('DOWNLOADS_AUTO_GENERATE_ON_ACCESS', 'false').lower() in ('1','true','yes') and archive_path and Path(archive_path).exists() and Path(archive_path).is_dir():
                try:
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("UPDATE download_tokens SET is_packing = TRUE WHERE token = %s;", (token,))
                        conn.commit()
                    from app.routes.api.downloads import process_directory_pack
                    t = threading.Thread(target=process_directory_pack, args=(token_data.get('stack_name'), archive_path, token), daemon=True)
                    t.start()
                    # Do not prefill any email in the preparing page
                    return render_template('download_preparing.html', token=token, stack_name=token_data.get('stack_name'), expires_at=token_data.get('expires_at')), 202
                except Exception as e:
                    logger.exception('Failed to start on-access generation for token %s: %s', token, e)
                    return render_template('download_error.html', reason='Download file could not be found and generation failed.', hint='Contact the administrator.'), 404

            return render_template('download_error.html', reason='Download file could not be found.', hint='Contact the administrator if this problem persists.'), 404
        
        # Serve the file using a normalized download filename
        # Ensure we treat file_path as a Path in case it's a string from the DB
        filename = utils.make_download_filename(Path(file_path).name)
        return send_file(
            str(file_path),
            as_attachment=True,
            download_name=filename,
            mimetype='application/octet-stream'
        )
        
    except Exception as e:
        logger.exception(f"Error serving download for token {token}: {e}")
        flash('Error processing download', 'danger')
        return redirect(url_for('dashboard.index'))


@app.route('/download/<token>/regenerate', methods=['POST'])
def download_regenerate(token):
    """Public endpoint: accept an email to notify and start regeneration if possible."""
    try:
        email = request.form.get('email', '').strip()
        # Validate single email entry
        if not email or ',' in email or ';' in email or ' ' in email or '@' not in email:
            return render_template('download_missing.html', token=token, stack_name='unknown', notify_emails=[], expires_at=None, error='Please provide a single valid email address'), 400

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT token, archive_path, stack_name, file_path, expires_at, is_packing, notify_emails FROM download_tokens WHERE token = %s;", (token,))
            row = cur.fetchone()
            if not row:
                return render_template('download_error.html', reason='Download token ungültig oder nicht gefunden.'), 404

            # Merge provided email into notify_emails array (unique)
            cur.execute("""
                UPDATE download_tokens
                SET notify_emails = (
                    SELECT ARRAY(SELECT DISTINCT e FROM (
                        SELECT unnest(COALESCE(notify_emails, ARRAY[]::text[])) UNION ALL SELECT unnest(%s::text[])
                    ) AS e)
                )
                WHERE token = %s;
            """, ([email], token))
            conn.commit()

            ap = row.get('archive_path') or row.get('file_path')
            if ap and Path(ap).exists() and Path(ap).is_dir() and not row.get('is_packing'):
                try:
                    # atomic update to avoid race: only set is_packing=TRUE if it was FALSE/NULL
                    cur.execute("UPDATE download_tokens SET is_packing = TRUE WHERE token = %s AND (is_packing = FALSE OR is_packing IS NULL) RETURNING token;", (token,))
                    r = cur.fetchone()
                    if r:
                        conn.commit()
                        from app.routes.api.downloads import process_directory_pack
                        t = threading.Thread(target=process_directory_pack, args=(row.get('stack_name'), ap, token), daemon=True)
                        t.start()
                    else:
                        # someone else started packing concurrently
                        # Do not prefill email input
                        return render_template('download_preparing.html', token=token, stack_name=row.get('stack_name'), expires_at=row.get('expires_at')), 202
                except Exception as e:
                    logger.exception('Failed to start regeneration for token %s: %s', token, e)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    return render_template('download_error.html', reason='Failed to start regeneration. Please contact the administrator.'), 500

        # Return preparing page without prefilled email
        return render_template('download_preparing.html', token=token, stack_name=row.get('stack_name'), expires_at=row.get('expires_at'))
    except Exception as e:
        logger.exception('Error handling regenerate form: %s', e)
        return render_template('download_error.html', reason='Interner Serverfehler'), 500

# Exempt public regenerate endpoint from CSRF protection
try:
    csrf.exempt(download_regenerate)
except Exception:
    pass

# Token-based download endpoint removed.


if __name__ == '__main__':
    # Initialize database
    init_db()
    
    # Run development server
    app.run(host='0.0.0.0', port=8080, debug=True)

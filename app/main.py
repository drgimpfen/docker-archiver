import os
import psycopg2
from psycopg2.extras import DictCursor
import threading
import time
from flask import Flask, render_template, render_template_string, request, redirect, url_for, flash, send_from_directory, session, jsonify
from jinja2 import TemplateNotFound
from datetime import datetime
from urllib.parse import urlparse
import subprocess
import base64

from werkzeug.security import generate_password_hash, check_password_hash
try:
    from webauthn import generate_registration_options, verify_registration_response, generate_authentication_options, verify_authentication_response
    from webauthn.helpers.structs import RegistrationCredential, AuthenticationCredential
except Exception as e:
    raise ImportError(
        "Failed to import required WebAuthn functions.\n"
        "Please ensure the correct WebAuthn library is installed (e.g. `webauthn` from PyPI).\n"
        "Current import error: " + str(e)
    )

import archive

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

# --- Configuration ---
DATABASE_URL = os.environ.get('DATABASE_URL')
LOCAL_STACKS_PATH = '/local'
CONTAINER_ARCHIVE_DIR = '/archives'  # Internal archive path inside the container

# WebAuthn Configuration
RP_ID = 'localhost'  # Relying Party ID - should be your domain in production
RP_NAME = 'Docker Archiver'
ORIGIN = 'http://localhost:5000' # Your full origin URL


def get_db_connection():
    """Establishes a connection to the database."""
    database_url = DATABASE_URL
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is not set.")
    # allow a short connect timeout so init attempts fail fast and can be retried
    connect_timeout = int(os.environ.get('DB_CONNECT_TIMEOUT', '5'))
    conn = psycopg2.connect(database_url, connect_timeout=connect_timeout)
    return conn

def get_user_count():
    """Returns the number of users in the database."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(id) FROM users;")
        count = cur.fetchone()[0]
    conn.close()
    return count

def get_user(username):
    """Gets a user from the database by username."""
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT id, username, password_hash, email, display_name FROM users WHERE username = %s;", (username,))
        user = cur.fetchone()
    conn.close()
    return user

def get_user_data_by_id(user_id):
    """Gets a user from the database by their ID."""
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT id, username, password_hash, email, display_name FROM users WHERE id = %s;", (user_id,))
        user = cur.fetchone()
    conn.close()
    return user


def init_db():
    """Initializes the database schema if it doesn't exist."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS archive_jobs (
                id SERIAL PRIMARY KEY,
                stack_name VARCHAR(255) NOT NULL,
                archive_path VARCHAR(1024),
                archive_size_bytes BIGINT,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration_seconds INTEGER,
                status VARCHAR(50) NOT NULL, -- (e.g., 'Running', 'Success', 'Failed')
                log TEXT
            );
        """)
        # Per-job sequence id (human-friendly incremental job id)
        cur.execute("CREATE SEQUENCE IF NOT EXISTS job_counter START 1;")
        # Add job_id and job_type columns if missing
        cur.execute("ALTER TABLE archive_jobs ADD COLUMN IF NOT EXISTS job_id INTEGER;")
        cur.execute("ALTER TABLE archive_jobs ALTER COLUMN job_id SET DEFAULT nextval('job_counter');")
        cur.execute("ALTER TABLE archive_jobs ADD COLUMN IF NOT EXISTS job_type VARCHAR(50) DEFAULT 'manual';")
        # Mark archive (group) jobs explicitly so UI can show only top-level archives when desired
        cur.execute("ALTER TABLE archive_jobs ADD COLUMN IF NOT EXISTS is_archive BOOLEAN DEFAULT FALSE;")
        # Add archive_id to link per-stack jobs to their archive run
        cur.execute("ALTER TABLE archive_jobs ADD COLUMN IF NOT EXISTS archive_id INTEGER;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(100) PRIMARY KEY,
                value VARCHAR(255) NOT NULL
            );
        """)
        # Table for persisted stack metadata (name + assigned color)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stacks (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) UNIQUE NOT NULL,
                color VARCHAR(16) NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                email VARCHAR(255) UNIQUE,
                display_name VARCHAR(255)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS passkeys (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                credential_id BYTEA UNIQUE NOT NULL,
                public_key BYTEA NOT NULL,
                sign_count INTEGER NOT NULL,
                transports VARCHAR(255)
            );
        """)
        # Add theme column to users for storing user theme preference (dark/light)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS theme VARCHAR(20) DEFAULT 'dark';")
        # Schedules table for automated backups
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                time VARCHAR(10) NOT NULL, -- HH:MM in 24h
                stack_paths TEXT NOT NULL, -- newline-separated list of paths
                retention_days INTEGER DEFAULT 28,
                enabled BOOLEAN DEFAULT TRUE,
                last_run TIMESTAMP
            );
        """)
        # Add optional description column to schedules
        cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS description TEXT;")
        # Add a type column to schedules so a schedule can be 'archive' or 'cleanup'
        cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS type VARCHAR(20) DEFAULT 'archive';")
        # Add store_unpacked flag to schedules (do we keep unpacked directory snapshots)
        cur.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS store_unpacked BOOLEAN DEFAULT FALSE;")
        # Set default retention if not present
        cur.execute("INSERT INTO settings (key, value) VALUES ('retention_days', '28') ON CONFLICT (key) DO NOTHING;")
        # Default: enable HTML in Apprise notifications unless explicitly disabled
        cur.execute("INSERT INTO settings (key, value) VALUES ('apprise_html', 'true') ON CONFLICT (key) DO NOTHING;")
        conn.commit()
    conn.close()

def initialize_app():
    """Initialize application resources with retries (runs in background).

    This avoids blocking Gunicorn workers when Postgres isn't immediately reachable.
    """

    def _init_worker():
        max_attempts = int(os.environ.get('DB_INIT_ATTEMPTS', '6'))
        attempt = 0
        backoff = 1
        while attempt < max_attempts:
            try:
                init_db()
                print("[init] Database initialization succeeded.")
                return
            except Exception as e:
                attempt += 1
                print(f"[init] Database init attempt {attempt} failed: {e}")
                if attempt >= max_attempts:
                    print("[init] Reached max DB init attempts; continuing without DB initialized.")
                    return
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    try:
        threading.Thread(target=_init_worker, daemon=True).start()
    except Exception:
        # Last resort synchronous attempt
        init_db()

# Start initialization in background at import time
initialize_app()


# --- Scheduler (APScheduler) ---
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import threading as _threading


def _job_runner(schedule_id):
    """Wrapper that loads schedule by id, updates last_run and triggers archive."""
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT * FROM schedules WHERE id = %s;", (schedule_id,))
            s = cur.fetchone()
        conn.close()

        if not s or not s.get('enabled'):
            return

        schedule_type = (s.get('type') or 'archive').lower()
        stack_paths = [p for p in (s.get('stack_paths') or '').split('\n') if p.strip()]
        retention = s.get('retention_days') or 28

        # update last_run
        now = datetime.now()
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE schedules SET last_run = %s WHERE id = %s;", (now, schedule_id))
            conn.commit()
        conn.close()

        # pass schedule name and description to archive/cleanup as archive_name/archive_description
        archive_name = s.get('name')
        archive_description = s.get('description') if s.get('description') else None
        if schedule_type == 'cleanup':
            # For cleanup schedules, run cleanup for this schedule only
            threading.Thread(target=archive.run_cleanup_job, args=(CONTAINER_ARCHIVE_DIR, archive_name, archive_description, schedule_id), daemon=True).start()
        else:
            store_unpacked_flag = bool(s.get('store_unpacked'))
            # Pass schedule name as schedule_label so archives are grouped under /archives/<schedule_name>/
            threading.Thread(target=archive.run_archive_job, args=(stack_paths, retention, CONTAINER_ARCHIVE_DIR, archive_name, archive_description, archive_name, store_unpacked_flag, 'scheduled'), daemon=True).start()
    except Exception as e:
        print(f"[scheduler] job_runner error for schedule {schedule_id}: {e}")


def _schedule_db_job(scheduler, s):
    """Register a schedule `s` (dict) with APScheduler."""
    try:
        time_val = (s.get('time') or '00:00').strip()
        hh, mm = (int(x) for x in time_val.split(':'))
    except Exception:
        return

    job_id = f"schedule_{s['id']}"
    try:
        scheduler.add_job(
            func=_job_runner,
            trigger='cron',
            hour=hh,
            minute=mm,
            args=(s['id'],),
            id=job_id,
            replace_existing=True,
        )
    except Exception as e:
        print(f"[scheduler] failed to add job {job_id}: {e}")


def _load_and_schedule_all(scheduler):
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT * FROM schedules WHERE enabled = true;")
            rows = cur.fetchall()
        conn.close()
        cleanup_scheduled = False
        for s in rows:
            try:
                if (s.get('type') or 'archive').lower() == 'cleanup':
                    if cleanup_scheduled:
                        # skip any additional cleanup schedules
                        continue
                    cleanup_scheduled = True
                _schedule_db_job(scheduler, s)
            except Exception:
                continue
    except Exception as e:
        print(f"[scheduler] load failed: {e}")


def _start_scheduler():
    tz_name = os.environ.get('TZ', 'UTC')
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        try:
            tz = ZoneInfo('UTC')
        except Exception:
            tz = None

    scheduler = BackgroundScheduler(timezone=tz)
    try:
        scheduler.start()
    except Exception as e:
        print(f"[scheduler] could not start: {e}")
        return None

    # Load schedules from DB and schedule enabled ones
    _load_and_schedule_all(scheduler)
    return scheduler


# start APScheduler in background
try:
    _SCHEDULER = _start_scheduler()
except Exception:
    _SCHEDULER = None

def get_setting(key):
    """Gets a value from the settings table."""
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT value FROM settings WHERE key = %s;", (key,))
        result = cur.fetchone()
    conn.close()
    return result['value'] if result else None


def format_duration(seconds):
    """Format seconds into H:MM:SS or M:SS."""
    try:
        s = int(seconds)
        hrs, rem = divmod(s, 3600)
        mins, secs = divmod(rem, 60)
        if hrs:
            return f"{hrs}:{mins:02d}:{secs:02d}"
        return f"{mins}:{secs:02d}"
    except Exception:
        return None


def _hsl_to_hex(h, s, l):
    """Convert HSL (0-360,0-1,0-1) to HEX color."""
    c = (1 - abs(2 * l - 1)) * s
    hp = h / 60.0
    x = c * (1 - abs((hp % 2) - 1))
    r1, g1, b1 = 0, 0, 0
    if 0 <= hp < 1:
        r1, g1, b1 = c, x, 0
    elif 1 <= hp < 2:
        r1, g1, b1 = x, c, 0
    elif 2 <= hp < 3:
        r1, g1, b1 = 0, c, x
    elif 3 <= hp < 4:
        r1, g1, b1 = 0, x, c
    elif 4 <= hp < 5:
        r1, g1, b1 = x, 0, c
    else:
        r1, g1, b1 = c, 0, x
    m = l - c / 2
    r, g, b = int((r1 + m) * 255), int((g1 + m) * 255), int((b1 + m) * 255)
    return '#{0:02x}{1:02x}{2:02x}'.format(r, g, b)


def _generate_color_for_name(name: str) -> str:
    """Deterministically generate a pleasant color from a name using its hash."""
    try:
        h = abs(hash(name)) % 360
    except Exception:
        h = 200
    s = 0.55
    l = 0.45
    return _hsl_to_hex(h, s, l)


def _text_color_for_bg(hex_color: str) -> str:
    """Return '#000' or '#fff' for readable contrast against given hex background."""
    try:
        hex_color = hex_color.lstrip('#')
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        # relative luminance
        lum = (0.2126 * (r/255.0) + 0.7152 * (g/255.0) + 0.0722 * (b/255.0))
        return '#000000' if lum > 0.6 else '#ffffff'
    except Exception:
        return '#ffffff'


def get_or_create_stack_color(stack_name: str):
    """Get persisted color for a stack or create an entry with a generated color."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT color FROM stacks WHERE name = %s;", (stack_name,))
            row = cur.fetchone()
            if row and row.get('color'):
                return row.get('color')
            # generate deterministic color and persist
            color = _generate_color_for_name(stack_name)
            try:
                cur.execute("INSERT INTO stacks (name, color) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE SET color = stacks.color;", (stack_name, color))
                conn.commit()
            except Exception:
                # ignore DB insert error and just return color
                pass
            return color
    finally:
        conn.close()


def _ordinal(n: int) -> str:
    """Return ordinal string for an integer (e.g. 1 -> '1st', 2 -> '2nd')."""
    try:
        n = int(n)
    except Exception:
        return str(n)
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


def format_start_time_ordinal(dt):
    """Format datetime as '16th December 2025 21:10' (24h)."""
    if not dt:
        return None
    day = _ordinal(dt.day)
    month = dt.strftime('%B')
    year = dt.year
    time_part = dt.strftime('%H:%M')
    return f"{day} {month} {year} - {time_part}"


def _serialize_webauthn_obj(obj):
    """Recursively serialize a WebAuthn options object to JSON-serializable primitives."""
    if obj is None:
        return None
    # bytes -> base64
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(obj)).decode('utf-8')
    # primitives
    if isinstance(obj, (str, int, float, bool)):
        return obj
    # dict
    if isinstance(obj, dict):
        # Convert snake_case keys to camelCase recursively so the browser
        # receives fields like `pubKeyCredParams`, `excludeCredentials`,
        # and `user.displayName` which the WebAuthn API requires.
        def snake_to_camel(s):
            parts = s.split('_')
            return parts[0] + ''.join(p.title() for p in parts[1:]) if len(parts) > 1 else s

        out = {}
        for k, v in obj.items():
            mapped = snake_to_camel(k)
            out[mapped] = _serialize_webauthn_obj(v)
        return out
    # list/tuple
    if isinstance(obj, (list, tuple)):
        return [_serialize_webauthn_obj(v) for v in obj]
    # objects with __dict__
    if hasattr(obj, '__dict__'):
        data = {}
        for k, v in vars(obj).items():
            if k.startswith('_'):
                continue
            # Some WebAuthn objects expose internal hint structures that browsers
            # don't accept; skip 'hints' but keep other fields (even if None)
            if k == 'hints':
                continue
            try:
                data[k] = _serialize_webauthn_obj(v)
            except Exception:
                data[k] = str(v)
        return data
    # fallback
    return str(obj)

def update_setting(key, value):
    """Updates a value in the settings table."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = %s;", (key, value, value))
        conn.commit()
    conn.close()

def discover_stacks():
    """Discovers Docker Compose stacks in the /local directory.

    This uses PyYAML to parse compose files and will exclude the application itself
    by checking for build.context or service definitions that point into the app directory.
    """
    stacks = []
    if not os.path.isdir(LOCAL_STACKS_PATH):
        return stacks

    try:
        import yaml
    except Exception:
        yaml = None

    app_dir = os.path.abspath(os.getcwd())

    def _labels_indicate_exclude(labels):
        if not labels:
            return False
        # dict form
        if isinstance(labels, dict):
            for k, v in labels.items():
                key = str(k).lower()
                if key in ('docker-archiver.exclude', 'archiver.exclude', 'docker_archiver.exclude'):
                    if str(v).lower() in ('1', 'true', 'yes', 'on'):
                        return True
            return False
        # list form like ["archiver.exclude=true"]
        if isinstance(labels, (list, tuple)):
            for item in labels:
                try:
                    if not isinstance(item, str):
                        continue
                    if '=' in item:
                        k, v = item.split('=', 1)
                        if k.strip().lower() in ('docker-archiver.exclude', 'archiver.exclude', 'docker_archiver.exclude') and v.strip().lower() in ('1', 'true', 'yes', 'on'):
                            return True
                except Exception:
                    continue
        return False

    def _find_compose(path):
        for fname in ('docker-compose.yml', 'compose.yaml'):
            candidate = os.path.join(path, fname)
            if os.path.exists(candidate):
                return candidate
        return None

    for volume_dir in os.listdir(LOCAL_STACKS_PATH):
        base_path = os.path.join(LOCAL_STACKS_PATH, volume_dir)
        if not os.path.isdir(base_path):
            continue

        # Case A: the mounted folder itself is a stack (contains compose file)
        compose_in_base = _find_compose(base_path)
        if compose_in_base:
            stack_path = base_path
            stack_dir = os.path.basename(stack_path)

            # By default do not include stacks that are the same directory as the app
            try:
                if os.path.abspath(stack_path) == app_dir:
                    continue
            except Exception:
                pass

            is_self = False
            if yaml is not None:
                try:
                    with open(compose_in_base, 'r', encoding='utf-8') as cf:
                        doc = yaml.safe_load(cf)
                        services = doc.get('services') if isinstance(doc, dict) else None
                        if isinstance(services, dict):
                            for svc_name, svc_def in services.items():
                                try:
                                    svc_labels = svc_def.get('labels') if isinstance(svc_def, dict) else None
                                except Exception:
                                    svc_labels = None
                                if _labels_indicate_exclude(svc_labels):
                                    is_self = True
                                    break

                                build = svc_def.get('build') if isinstance(svc_def, dict) else None
                                if isinstance(build, dict):
                                    ctx = build.get('context')
                                    if ctx:
                                        abs_ctx = os.path.abspath(os.path.join(stack_path, ctx)) if not os.path.isabs(ctx) else os.path.abspath(ctx)
                                        if abs_ctx.startswith(app_dir):
                                            is_self = True
                                            break
                                elif isinstance(build, str):
                                    abs_ctx = os.path.abspath(os.path.join(stack_path, build)) if not os.path.isabs(build) else os.path.abspath(build)
                                    if abs_ctx.startswith(app_dir):
                                        is_self = True
                                        break
                        top_labels = doc.get('labels') if isinstance(doc, dict) else None
                        if _labels_indicate_exclude(top_labels):
                            is_self = True
                except Exception:
                    pass

            if not is_self:
                stacks.append({'name': stack_dir, 'path': stack_path})
            # don't search deeper if base itself is a stack
            continue

        # Case B: look one level deep for stacks inside subfolders
        for stack_dir in os.listdir(base_path):
            stack_path = os.path.join(base_path, stack_dir)
            if not os.path.isdir(stack_path):
                continue

            compose_path = _find_compose(stack_path)
            if not compose_path:
                continue

            try:
                if os.path.abspath(stack_path) == app_dir:
                    continue
            except Exception:
                pass

            is_self = False
            if yaml is not None:
                try:
                    with open(compose_path, 'r', encoding='utf-8') as cf:
                        doc = yaml.safe_load(cf)
                        services = doc.get('services') if isinstance(doc, dict) else None
                        if isinstance(services, dict):
                            for svc_name, svc_def in services.items():
                                try:
                                    svc_labels = svc_def.get('labels') if isinstance(svc_def, dict) else None
                                except Exception:
                                    svc_labels = None
                                if _labels_indicate_exclude(svc_labels):
                                    is_self = True
                                    break

                                build = svc_def.get('build') if isinstance(svc_def, dict) else None
                                if isinstance(build, dict):
                                    ctx = build.get('context')
                                    if ctx:
                                        abs_ctx = os.path.abspath(os.path.join(stack_path, ctx)) if not os.path.isabs(ctx) else os.path.abspath(ctx)
                                        if abs_ctx.startswith(app_dir):
                                            is_self = True
                                            break
                                elif isinstance(build, str):
                                    abs_ctx = os.path.abspath(os.path.join(stack_path, build)) if not os.path.isabs(build) else os.path.abspath(build)
                                    if abs_ctx.startswith(app_dir):
                                        is_self = True
                                        break
                        top_labels = doc.get('labels') if isinstance(doc, dict) else None
                        if _labels_indicate_exclude(top_labels):
                            is_self = True
                except Exception:
                    pass

            if is_self:
                continue

            stacks.append({'name': stack_dir, 'path': stack_path})

    return sorted(stacks, key=lambda s: s['name'])


def discover_stacks_with_timeout(timeout_seconds=2):
    """Call `discover_stacks()` but return empty list on timeout to avoid blocking request threads."""
    result = []
    def _worker():
        nonlocal result
        try:
            result = discover_stacks()
        except Exception:
            result = []

    th = _threading.Thread(target=_worker)
    th.daemon = True
    th.start()
    th.join(timeout_seconds)
    if th.is_alive():
        return []
    return result


@app.before_request
def check_auth():
    """Checks authentication before each request."""
    # Endpoints that don't require authentication
    allowed_endpoints = [
        'login_route', 'setup_route', 'logout_route', # 'register_route' removed
        'generate_authentication_options_route', 'verify_authentication_route', # Passkey login
    ]
    if request.path.startswith('/static/'):
        return

    # If no user exists, allow only setup
    if get_user_count() == 0:
        if request.endpoint == 'setup_route':
            return
        return redirect(url_for('setup_route'))

    # If user exists but is not logged in, allow only login-related routes
    if 'user_id' not in session:
        if request.endpoint in allowed_endpoints:
            return
        return redirect(url_for('login_route'))

@app.route('/setup', methods=['GET', 'POST'])
def setup_route():
    """Handles the initial setup of the admin user if no users exist."""
    if get_user_count() > 0:
        flash('Admin user already exists. Please log in.', 'warning')
        return redirect(url_for('login_route'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        email = request.form.get('email')
        display_name = request.form.get('display_name')

        if not username or not password:
            flash('Username and Password are required.', 'danger')
            return render_template('setup_initial_user.html', username=username, email=email, display_name=display_name)
        
        password_hash = generate_password_hash(password)
        
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, email, display_name) VALUES (%s, %s, %s, %s) RETURNING id;",
                (username, password_hash, email, display_name)
            )
            user_id = cur.fetchone()[0]
            conn.commit()
        conn.close()
        
        session['user_id'] = user_id
        flash('Admin user created successfully! You are now logged in.', 'success')
        return redirect(url_for('index'))

    # For GET (or if POST failed validation) render the setup template or a small fallback
    try:
        return render_template('setup_initial_user.html')
    except TemplateNotFound:
        fallback_html = '''
        <!doctype html>
        <html><head><meta charset="utf-8"><title>Initial Setup</title></head>
        <body style="font-family:Arial,Helvetica,sans-serif;margin:24px;">
            <h2>Initial Setup</h2>
            <p>The setup template is missing from the container. You can still create the first admin user using the form below.</p>
            <form method="post">
                <label>Username:<br><input name="username" required></label><br><br>
                <label>Password:<br><input name="password" type="password" required></label><br><br>
                <label>Email (optional):<br><input name="email" type="email"></label><br><br>
                <label>Display name (optional):<br><input name="display_name"></label><br><br>
                <button type="submit">Create Admin User</button>
            </form>
            <p style="margin-top:16px;color:#666">Tip: For development, mount the local <code>app/</code> folder into the container or copy the missing template into the container.</p>
        </body></html>
        '''
        return render_template_string(fallback_html)

@app.route('/login', methods=['GET', 'POST'])
def login_route():
    if 'user_id' in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        user = get_user(username)
        
        if not user or not check_password_hash(user['password_hash'], password):
            flash('Invalid username or password.', 'error')
            return redirect(url_for('login_route'))
        
        session['user_id'] = user['id']
        return redirect(url_for('index'))

    return render_template('login.html')

@app.route('/logout')
def logout_route():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login_route'))

def get_user_passkeys(user_id):
    """Gets all passkeys for a given user."""
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT * FROM passkeys WHERE user_id = %s;", (user_id,))
        keys = cur.fetchall()
    conn.close()
    return keys

def update_user_password(user_id, new_password_hash):
    """Updates the user's password hash in the database."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s;", (new_password_hash, user_id))
        conn.commit()
    conn.close()

def update_user_email(user_id, new_email):
    """Updates the user's email in the database."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET email = %s WHERE id = %s;", (new_email, user_id))
        conn.commit()
    conn.close()

def update_user_display_name(user_id, new_display_name):
    """Updates the user's display name in the database."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET display_name = %s WHERE id = %s;", (new_display_name, user_id))
        conn.commit()
    conn.close()

@app.route('/profile')
def profile_route():
    """Displays the user's profile page for managing passkeys and user details."""
    user_id = session['user_id']
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT id, username, email, display_name FROM users WHERE id = %s;", (user_id,))
        user_data = cur.fetchone()
    conn.close()
    
    passkeys = get_user_passkeys(user_id)
    
    display_passkeys = []
    for key in passkeys:
        display_passkeys.append({
            'id': key['id'],
            'credential_id_b64': base64.b64encode(key['credential_id']).decode('utf-8'),
        })

    return render_template('profile.html', user=user_data, passkeys=display_passkeys)


@app.context_processor
def inject_user_theme():
    """Injects the user's theme preference into templates as `user_theme`."""
    theme = None
    user_id = session.get('user_id')
    if user_id:
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute("SELECT theme FROM users WHERE id = %s;", (user_id,))
                row = cur.fetchone()
            conn.close()
            if row and row.get('theme'):
                theme = row.get('theme')
        except Exception:
            theme = None
    return dict(user_theme=theme)

@app.route('/profile/edit', methods=['GET', 'POST'])
def profile_edit_route():
    """Allows the user to edit their email and display name."""
    user_id = session['user_id']
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT id, username, email, display_name FROM users WHERE id = %s;", (user_id,))
        user_data = cur.fetchone()
    conn.close()

    if request.method == 'POST':
        new_email = request.form['email']
        new_display_name = request.form['display_name']
        new_theme = request.form.get('theme')

        if user_data['email'] != new_email:
            update_user_email(user_id, new_email)
            flash('Email updated successfully!', 'success')
        
        if user_data['display_name'] != new_display_name:
            update_user_display_name(user_id, new_display_name)
            flash('Display name updated successfully!', 'success')

        if new_theme and (user_data.get('theme') != new_theme):
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET theme = %s WHERE id = %s;", (new_theme, user_id))
                conn.commit()
            conn.close()
            flash('Theme preference updated!', 'success')
        
        if user_data['email'] == new_email and user_data['display_name'] == new_display_name:
            flash('No changes detected.', 'info')

        return redirect(url_for('profile_route'))
        
    return render_template('profile_edit.html', user=user_data)


@app.route('/settings', methods=['GET', 'POST'])
def settings_route():
    """Global settings page for system-level configuration (notifications etc.)."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_route'))

    # Load current settings
    apprise_urls = get_setting('apprise_urls') or ''
    apprise_enabled = get_setting('apprise_enabled')
    apprise_enabled_bool = (str(apprise_enabled).lower() == 'true') if apprise_enabled is not None else False
    apprise_html = get_setting('apprise_html')
    apprise_html_bool = (str(apprise_html).lower() == 'true') if apprise_html is not None else True
    app_base_url = get_setting('app_base_url') or ''

    if request.method == 'POST':
        apprise_enabled_form = request.form.get('apprise_enabled', 'false')
        apprise_urls_form = request.form.get('apprise_urls', '').strip()
        apprise_html_form = request.form.get('apprise_html', 'true')
        app_base_url_form = request.form.get('app_base_url', '').strip()

        # canonicalize app_base_url: ensure scheme present and strip trailing slash
        canonical_base = ''
        if app_base_url_form:
            try:
                p = urlparse(app_base_url_form)
                if not p.scheme:
                    # default to http when scheme missing
                    app_base_url_form = 'http://' + app_base_url_form
                    p = urlparse(app_base_url_form)
                # rebuild minimal canonical form
                canonical_base = f"{p.scheme}://{p.netloc.rstrip('/')}{p.path.rstrip('/')}"
            except Exception:
                canonical_base = app_base_url_form.rstrip('/')

        update_setting('apprise_enabled', 'true' if apprise_enabled_form == 'true' else 'false')
        update_setting('apprise_urls', apprise_urls_form)
        update_setting('apprise_html', 'true' if apprise_html_form == 'true' else 'false')
        update_setting('app_base_url', canonical_base)

        flash('Settings saved.', 'success')
        return redirect(url_for('settings_route'))

    return render_template('settings.html', apprise_urls=apprise_urls, apprise_enabled=apprise_enabled_bool, apprise_html=apprise_html_bool, app_base_url=app_base_url)


@app.context_processor
def inject_app_base_url():
    """Inject the configured App Base URL into templates as `app_base_url` (prefers DB setting)."""
    try:
        base = get_setting('app_base_url') or os.environ.get('APP_BASE_URL', '')
        base = (base or '').rstrip('/')
    except Exception:
        base = ''
    return dict(app_base_url=base)

@app.route('/profile/change_password', methods=['GET', 'POST'])
def profile_change_password_route():
    """Allows the user to change their password."""
    user_id = session['user_id']
    user_data = get_user_data_by_id(user_id)

    if request.method == 'POST':
        current_password = request.form['current_password']
        new_password = request.form['new_password']
        confirm_new_password = request.form['confirm_new_password']

        if not check_password_hash(user_data['password_hash'], current_password):
            flash('Incorrect current password.', 'danger')
        elif new_password != confirm_new_password:
            flash('New password and confirmation do not match.', 'danger')
        else:
            new_password_hash = generate_password_hash(new_password)
            update_user_password(user_id, new_password_hash)
            flash('Password changed successfully!', 'success')
            return redirect(url_for('profile_route'))
            
    return render_template('profile_change_password.html')

# --- Passkey Routes ---
@app.route('/webauthn/generate-registration-options', methods=['POST'])
def generate_registration_options_route():
    return jsonify({"error": "Passkey / WebAuthn registration is temporarily disabled."}), 501
    

@app.route('/webauthn/generate-authentication-options', methods=['POST'])
def generate_authentication_options_route():
    return jsonify({"error": "Passkey / WebAuthn authentication is temporarily disabled."}), 501

@app.route('/webauthn/verify-authentication', methods=['POST'])
def verify_authentication_route():
    return jsonify({"error": "Passkey / WebAuthn authentication is temporarily disabled."}), 501

@app.route('/webauthn/delete-passkey/<int:passkey_id>', methods=['POST'])
def delete_passkey_route(passkey_id):
    """Deletes a passkey for the logged-in user."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_route'))
    flash('Passkey functionality is disabled. Deletion is not available.', 'warning')
    return redirect(url_for('profile_route'))


@app.route('/')
def index():
    """Main dashboard page."""
    stacks = discover_stacks()
    retention_days = get_setting('retention_days')
    
    conn = get_db_connection()
    recent_jobs = []
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            # Get last 10 top-level archive runs only
            cur.execute("SELECT * FROM archive_jobs WHERE is_archive = true ORDER BY start_time DESC LIMIT 10;")
            masters = cur.fetchall()
            for m in masters:
                # fetch child stack rows for this archive (include sizes)
                cur.execute("SELECT stack_name, archive_size_bytes, duration_seconds FROM archive_jobs WHERE archive_id = %s ORDER BY start_time ASC;", (m.get('id'),))
                children = cur.fetchall()
                stacks = []
                total_size_bytes = 0
                if children:
                    for c in children:
                        sname = c.get('stack_name')
                        color = get_or_create_stack_color(sname)
                        text_color = _text_color_for_bg(color)
                        stacks.append({'name': sname, 'color': color, 'text_color': text_color})
                        try:
                            sz = int(c.get('archive_size_bytes')) if c.get('archive_size_bytes') is not None else 0
                            total_size_bytes += sz
                        except Exception:
                            pass
                # format start time in long form and duration human readable
                start_time = m.get('start_time')
                start_time_str = format_start_time_ordinal(start_time) if start_time else None
                duration_human = format_duration(m.get('duration_seconds')) if m.get('duration_seconds') is not None else 'N/A'
                recent_jobs.append({
                    'id': m.get('id'),
                    'archive_seq': m.get('job_id'),
                    'type': (m.get('job_type') or 'manual'),
                    'name': m.get('stack_name'),
                    'stacks': stacks,
                    'status': m.get('status'),
                    'started': start_time,
                    'started_str': start_time_str,
                    'size_bytes': total_size_bytes,
                    'size_human': format_bytes(total_size_bytes) if total_size_bytes else 'N/A',
                    'duration_human': duration_human,
                })
    finally:
        conn.close()

    # expose whether a cleanup is currently in progress so the UI can disable the manual trigger
    cleanup_flag = get_setting('cleanup_in_progress')
    cleanup_in_progress = True if (cleanup_flag and str(cleanup_flag).lower() == 'true') else False

    # default cleanup description auto-fill: 'Manual Cleanup by <username> at <timestamp>'
    default_cleanup_description = ''
    user_id = session.get('user_id')
    if user_id:
        try:
            user = get_user_data_by_id(user_id)
            uname = user.get('display_name') or user.get('username')
        except Exception:
            uname = None
    else:
        uname = None

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if uname:
        default_cleanup_description = f"Manual Cleanup by {uname} at {ts}"
    else:
        default_cleanup_description = f"Manual Cleanup at {ts}"

    return render_template('index.html', stacks=stacks, retention_days=retention_days, recent_jobs=recent_jobs, cleanup_in_progress=cleanup_in_progress, default_cleanup_description=default_cleanup_description)

@app.route('/archive', methods=['POST'])
def start_archive_route():
    """Starts the archiving process in a background thread."""
    selected_stack_paths = request.form.getlist('stacks')

    if not selected_stack_paths:
        flash('Please select at least one stack to archive.', 'warning')
        return redirect(url_for('index'))

    
    stack_names_for_flash = [os.path.basename(path) for path in selected_stack_paths]

    # Build archive name and optional manual label/description
    archive_name = request.form.get('manual_name')
    archive_description = request.form.get('manual_description')
    if not archive_name or not archive_name.strip():
        # fallback to automatic name containing stacks
        ts_label = datetime.now().strftime('%Y%m%d_%H%M%S')
        archive_name = f"manual_run_{ts_label}"
    else:
        # sanitize name for filesystem use
        archive_name = archive_name.strip()
        for ch in (' ', ':', '/', '\\', ',', '"', "'"):
            archive_name = archive_name.replace(ch, '_')

    # Start archive job and group files under the provided archive_name
    # Retention is managed in Settings; manual archive form does not change it
    retention_days = None
    thread = threading.Thread(
        target=archive.run_archive_job,
        args=(selected_stack_paths, retention_days, CONTAINER_ARCHIVE_DIR, archive_name, archive_description, archive_name, False, 'manual'),
        daemon=True
    )
    thread.start()

    flash(f"Archiving process started in the background for: {', '.join(stack_names_for_flash)} (group: {archive_name})", 'info')
    return redirect(url_for('index'))


@app.route('/archive')
def archive_route():
    """Renders the manual archive (create) page."""
    stacks = discover_stacks()
    return render_template('archive.html', stacks=stacks)


@app.route('/cleanup', methods=['POST'])
def start_cleanup_route():
    """Starts a global cleanup process in background applying retention of all enabled schedules."""
    # Prevent starting if a cleanup is already in progress
    try:
        if str(archive._get_setting('cleanup_in_progress')).lower() == 'true':
            flash('A cleanup is already running. Please wait for it to finish.', 'warning')
            return redirect(url_for('index'))
    except Exception:
        # If check fails, fall through and attempt start (best-effort)
        pass

    archive_name = 'Manual Cleanup'
    archive_description = request.form.get('description')
    thread = threading.Thread(target=archive.run_cleanup_job, args=(CONTAINER_ARCHIVE_DIR, archive_name, archive_description), daemon=True)
    thread.start()
    flash('Cleanup process started in the background.', 'info')
    return redirect(url_for('index'))


@app.route('/history')
def history():
    """Shows the full archive history."""
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        # Only show top-level archive runs in history (per-stack details are internal)
        cur.execute("SELECT * FROM archive_jobs WHERE is_archive = true ORDER BY start_time DESC;")
        all_jobs = cur.fetchall()
    conn.close()
    return render_template('history.html', jobs=all_jobs)


@app.route('/history/archive_children/<int:archive_id>')
def archive_children(archive_id):
    """Returns JSON array of per-stack jobs belonging to a top-level archive run."""
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "SELECT id, stack_name, start_time, end_time, duration_seconds, status, archive_size_bytes, archive_path, log FROM archive_jobs WHERE archive_id = %s ORDER BY start_time ASC;",
                (archive_id,)
            )
            rows = cur.fetchall()
        conn.close()
        results = []
        for r in rows:
            results.append({
                'id': r.get('id'),
                'stack_name': r.get('stack_name'),
                'start_time': r.get('start_time').strftime('%Y-%m-%d %H:%M:%S') if r.get('start_time') else None,
                'end_time': r.get('end_time').strftime('%Y-%m-%d %H:%M:%S') if r.get('end_time') else None,
                'duration_seconds': r.get('duration_seconds'),
                'status': r.get('status'),
                'archive_size_bytes': r.get('archive_size_bytes'),
                'archive_path': r.get('archive_path'),
                'log': r.get('log')
            })
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/job_log/<int:job_id>')
def job_log(job_id):
    """Return the raw log text for a specific job id as JSON."""
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT log FROM archive_jobs WHERE id = %s;", (job_id,))
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({'error': 'Job not found'}), 404
        return jsonify({'log': row.get('log') or ''})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def format_bytes(size):
    """Formats a size in bytes to a human-readable string."""
    # This helper function can be placed anywhere, e.g., in a separate utility file or here.
    power = 2**10
    n = 0
    power_labels = {0 : '', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}"

def scan_archives():
    """Scans the archive directory and returns a structured dictionary with extended info."""
    result = {
        'scheduled': [],  # each: {name, stacks: {stack_name: {...}}}
        'manual': []      # manual runs grouped by name (each: {name, stacks: {...}})
    }

    if not os.path.isdir(CONTAINER_ARCHIVE_DIR):
        return result

    # load known schedule names from DB so we can distinguish scheduled vs manual runs
    schedule_names = set()
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM schedules;")
            rows = cur.fetchall()
            for r in rows:
                try:
                    schedule_names.add(str(r[0]))
                except Exception:
                    continue
        conn.close()
    except Exception:
        schedule_names = set()

    for top in sorted(os.listdir(CONTAINER_ARCHIVE_DIR)):
        top_path = os.path.join(CONTAINER_ARCHIVE_DIR, top)
        if not os.path.isdir(top_path):
            continue
        # Check if this top-level directory contains stack subdirectories
        sub_entries = [e for e in os.listdir(top_path) if os.path.isdir(os.path.join(top_path, e))]
        if not sub_entries:
            # Unexpected structure (no subdirs) — skip
            continue

        # Build stacks info for this top-level group
        group = {'name': top, 'stacks': {}}
        for stack_name in sorted(sub_entries):
            stack_dir = os.path.join(top_path, stack_name)
            archive_files = []
            total_size_bytes = 0
            last_backup_timestamp = 0
            for filename in sorted(os.listdir(stack_dir), reverse=True):
                if filename.endswith('.tar'):
                    file_path = os.path.join(stack_dir, filename)
                    try:
                        stat_info = os.stat(file_path)
                        # Base archive info from filesystem
                        entry = {
                            'name': filename,
                            'size': stat_info.st_size,
                            'created_at': datetime.fromtimestamp(stat_info.st_mtime),
                            'duration_seconds': None,
                            'archive_size_bytes_db': None,
                            'archive_path_db': None
                        }
                        # Try to enrich from DB: find a matching archive_jobs row by archive_path ending with this file
                        try:
                            conn = get_db_connection()
                            with conn.cursor(cursor_factory=DictCursor) as cur:
                                # Look for a recent job with archive_path ending in the expected path
                                expected_suffix = os.path.join(top, stack_name, filename).replace('\\', '/')
                                cur.execute("SELECT id, archive_size_bytes, duration_seconds, archive_path FROM archive_jobs WHERE archive_path LIKE %s ORDER BY start_time DESC LIMIT 1;", ('%' + expected_suffix,))
                                row = cur.fetchone()
                                if row:
                                    entry['job_id'] = row.get('id')
                                    entry['archive_size_bytes_db'] = row.get('archive_size_bytes')
                                    entry['duration_seconds'] = row.get('duration_seconds')
                                    entry['duration_human'] = format_duration(row.get('duration_seconds')) if row.get('duration_seconds') is not None else None
                                    entry['archive_path_db'] = row.get('archive_path')
                            conn.close()
                        except Exception:
                            try:
                                conn.close()
                            except Exception:
                                pass
                        archive_files.append(entry)
                        total_size_bytes += stat_info.st_size
                        if stat_info.st_mtime > last_backup_timestamp:
                            last_backup_timestamp = stat_info.st_mtime
                    except OSError:
                        continue
            if archive_files:
                group['stacks'][stack_name] = {
                    'count': len(archive_files),
                    'total_size_bytes': total_size_bytes,
                    'total_size_human': format_bytes(total_size_bytes),
                    'last_backup': datetime.fromtimestamp(last_backup_timestamp) if last_backup_timestamp else None,
                    'files': archive_files
                }

        if not group['stacks']:
            continue

        # Compute group's latest timestamp for default-open selection
        group_max_ts = 0
        for s_info in group['stacks'].values():
            try:
                if s_info.get('last_backup'):
                    ts = s_info['last_backup'].timestamp()
                    if ts > group_max_ts:
                        group_max_ts = ts
            except Exception:
                continue
        group['__last_ts'] = group_max_ts

        # Enrich group with last master job info and total size (from filesystem)
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=DictCursor) as cur:
                # find latest top-level archive run for this group name
                cur.execute("SELECT start_time, end_time, duration_seconds FROM archive_jobs WHERE is_archive = true AND stack_name = %s ORDER BY start_time DESC LIMIT 1;", (top,))
                archive_row = cur.fetchone()
            conn.close()
            if archive_row:
                group['archive_start'] = archive_row.get('start_time')
                group['archive_end'] = archive_row.get('end_time')
                group['archive_duration_seconds'] = archive_row.get('duration_seconds')
            else:
                group['archive_start'] = None
                group['archive_end'] = None
                group['archive_duration_seconds'] = None
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

        group['total_size_bytes'] = total_size_bytes
        group['total_size_human'] = format_bytes(total_size_bytes)

        # Decide whether this top-level group is a scheduled run or a manual run
        try:
            if top in schedule_names:
                result['scheduled'].append(group)
            else:
                result['manual'].append(group)
        except Exception:
            result['manual'].append(group)

    # Sort groups so the newest group (by internal latest timestamp) appears first
    try:
        result['scheduled'].sort(key=lambda g: g.get('__last_ts', 0), reverse=True)
    except Exception:
        pass
    try:
        result['manual'].sort(key=lambda g: g.get('__last_ts', 0), reverse=True)
    except Exception:
        pass

    # Determine which group (scheduled/manual) to open by default: choose the group with the newest timestamp
    try:
        best_kind = None
        best_idx = None
        best_ts = 0
        # scheduled
        for i, g in enumerate(result.get('scheduled', [])):
            try:
                if g.get('__last_ts', 0) > best_ts:
                    best_ts = g.get('__last_ts', 0)
                    best_kind = 'scheduled'
                    best_idx = i
            except Exception:
                continue
        # manual
        for i, g in enumerate(result.get('manual', [])):
            try:
                if g.get('__last_ts', 0) > best_ts:
                    best_ts = g.get('__last_ts', 0)
                    best_kind = 'manual'
                    best_idx = i
            except Exception:
                continue
        if best_kind is not None:
            result['default_open'] = {'kind': best_kind, 'index': best_idx}
        else:
            result['default_open'] = None
    except Exception:
        result['default_open'] = None

    return result


@app.route('/archives')
def archives_list():
    """Shows a list of available archives, grouped by stack."""
    all_archives = scan_archives()
    return render_template('archives.html', archives=all_archives)


# Migration of top-level archives removed — no longer applicable


@app.route('/schedules', methods=['GET', 'POST'])
def schedules_route():
    """View and create schedules."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_route'))

    # Listing only; creation/editing handled by separate endpoints

    stacks = discover_stacks_with_timeout()
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT * FROM schedules ORDER BY id DESC;")
        schedules = cur.fetchall()
    conn.close()

    return render_template('schedules.html', stacks=stacks, schedules=schedules)


@app.route('/schedules/create', methods=['POST'])
def create_schedule_route():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_route'))

    name = request.form.get('name')
    time_val = request.form.get('time')
    description = request.form.get('description')
    stacks = request.form.getlist('stacks')
    retention_days = int(request.form.get('retention_days') or 28)
    schedule_type = request.form.get('type') or 'archive'
    store_unpacked = True if request.form.get('store_unpacked') == 'on' else False
    stack_text = '\n'.join(stacks)
    # Prevent creating more than one cleanup schedule
    if schedule_type == 'cleanup':
        try:
            conn_check = get_db_connection()
            with conn_check.cursor() as cur_check:
                cur_check.execute("SELECT COUNT(1) FROM schedules WHERE type = 'cleanup';")
                existing = cur_check.fetchone()[0]
            conn_check.close()
            if existing and existing > 0:
                flash('A cleanup schedule already exists; only one cleanup schedule is allowed.', 'danger')
                return redirect(url_for('schedules_route'))
        except Exception:
            # If check fails, be conservative and block creation
            flash('Could not verify existing cleanup schedules; not creating a second cleanup schedule.', 'danger')
            return redirect(url_for('schedules_route'))
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, time, stack_paths, retention_days, enabled, description, type, store_unpacked) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
            (name, time_val, stack_text, retention_days, True, description, schedule_type, store_unpacked)
        )
        conn.commit()
    conn.close()
    flash('Schedule created.', 'success')
    # reload scheduler if running
    try:
        if _SCHEDULER is not None:
            # fetch full schedule row and register it
            new_id = get_last_insert_id()
            with get_db_connection().cursor(cursor_factory=DictCursor) as cur:
                cur.execute("SELECT * FROM schedules WHERE id = %s;", (new_id,))
                new_s = cur.fetchone()
            if new_s:
                _schedule_db_job(_SCHEDULER, new_s)
    except Exception:
        pass
    return redirect(url_for('schedules_route'))


def get_last_insert_id():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT currval(pg_get_serial_sequence('schedules','id'));")
            r = cur.fetchone()
            return r[0] if r else None
    finally:
        conn.close()


@app.route('/schedules/edit/<int:schedule_id>', methods=['GET', 'POST'])
def edit_schedule_route(schedule_id):
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_route'))

    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT * FROM schedules WHERE id = %s;", (schedule_id,))
        sch = cur.fetchone()
    conn.close()
    if not sch:
        flash('Schedule not found.', 'danger')
        return redirect(url_for('schedules_route'))

    if request.method == 'POST':
        name = request.form.get('name')
        time_val = request.form.get('time')
        description = request.form.get('description')
        schedule_type = request.form.get('type') or 'archive'
        store_unpacked = True if request.form.get('store_unpacked') == 'on' else False
        stacks = request.form.getlist('stacks')
        retention_days = int(request.form.get('retention_days') or 28)
        enabled = True if request.form.get('enabled') == 'true' else False

        stack_text = '\n'.join(stacks)
        # If switching this schedule to type 'cleanup', ensure no other cleanup schedule exists
        if schedule_type == 'cleanup':
            try:
                conn_check = get_db_connection()
                with conn_check.cursor() as cur_check:
                    cur_check.execute("SELECT id FROM schedules WHERE type = 'cleanup' AND id != %s;", (schedule_id,))
                    other = cur_check.fetchone()
                conn_check.close()
                if other:
                    flash('Another cleanup schedule already exists; cannot convert this schedule to cleanup.', 'danger')
                    return redirect(url_for('schedules_route'))
            except Exception:
                flash('Could not verify existing cleanup schedules; not updating to cleanup.', 'danger')
                return redirect(url_for('schedules_route'))

        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE schedules SET name=%s, time=%s, stack_paths=%s, retention_days=%s, enabled=%s, description=%s, type=%s, store_unpacked=%s WHERE id=%s;",
                        (name, time_val, stack_text, retention_days, enabled, description, schedule_type, store_unpacked, schedule_id))
            conn.commit()
        conn.close()
        # update scheduler
        try:
            if _SCHEDULER is not None:
                _schedule_db_job(_SCHEDULER, {'id': schedule_id, 'time': time_val})
        except Exception:
            pass
        flash('Schedule updated.', 'success')
        return redirect(url_for('schedules_route'))

    stacks = discover_stacks_with_timeout()
    return render_template('schedules_edit.html', sch=sch, stacks=stacks)


@app.route('/schedules/toggle/<int:schedule_id>', methods=['POST'])
def toggle_schedule_route(schedule_id):
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_route'))
    # Read current schedule to determine desired state and type
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT enabled, type FROM schedules WHERE id = %s;", (schedule_id,))
        sch = cur.fetchone()
    conn.close()
    if not sch:
        flash('Schedule not found.', 'danger')
        return redirect(url_for('schedules_route'))

    desired_enabled = not sch.get('enabled')
    sch_type = (sch.get('type') or 'archive').lower()
    # If enabling and this is a cleanup schedule, ensure no other cleanup schedule is enabled
    if desired_enabled and sch_type == 'cleanup':
        try:
            conn_check = get_db_connection()
            with conn_check.cursor() as cur_check:
                cur_check.execute("SELECT COUNT(1) FROM schedules WHERE type = 'cleanup' AND enabled = true AND id != %s;", (schedule_id,))
                cnt = cur_check.fetchone()[0]
            conn_check.close()
            if cnt and cnt > 0:
                flash('Another enabled cleanup schedule exists; cannot enable this cleanup schedule.', 'danger')
                return redirect(url_for('schedules_route'))
        except Exception:
            flash('Could not verify existing cleanup schedules; not toggling.', 'danger')
            return redirect(url_for('schedules_route'))

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = NOT enabled WHERE id = %s RETURNING enabled;", (schedule_id,))
        r = cur.fetchone()
        conn.commit()
    conn.close()
    # reload scheduler entry
    try:
        if _SCHEDULER is not None:
            with get_db_connection().cursor(cursor_factory=DictCursor) as cur:
                cur.execute("SELECT * FROM schedules WHERE id = %s;", (schedule_id,))
                s = cur.fetchone()
            if s and s.get('enabled'):
                _schedule_db_job(_SCHEDULER, s)
            else:
                try:
                    _SCHEDULER.remove_job(f"schedule_{schedule_id}")
                except Exception:
                    pass
    except Exception:
        pass
    flash('Schedule toggled.', 'success')
    return redirect(url_for('schedules_route'))


@app.route('/schedules/delete/<int:schedule_id>', methods=['POST'])
def delete_schedule_route(schedule_id):
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_route'))

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM schedules WHERE id = %s;", (schedule_id,))
        conn.commit()
    conn.close()
    flash('Schedule deleted.', 'info')
    return redirect(url_for('schedules_route'))


@app.route('/download_archive/<path:stack_name>/<path:archive_filename>')
def download_archive(stack_name, archive_filename):
    """Provides a download for a specific archive file."""
    # Construct the directory path for the given stack
    stack_archive_dir = os.path.join(os.path.abspath(CONTAINER_ARCHIVE_DIR), stack_name)
    
    # Ensure the directory exists and is inside the main archive directory
    if not os.path.isdir(stack_archive_dir) or not stack_archive_dir.startswith(os.path.abspath(CONTAINER_ARCHIVE_DIR)):
        flash('Archive directory not found.', 'danger')
        return redirect(url_for('archives_list'))

    return send_from_directory(
        stack_archive_dir,
        archive_filename,
        as_attachment=True
    )


@app.route('/delete_archive/<path:stack_name>/<path:archive_filename>', methods=['POST'])
def delete_archive(stack_name, archive_filename):
    """Deletes a specific archive file."""
    # Construct the full path to the archive file
    file_path = os.path.join(CONTAINER_ARCHIVE_DIR, stack_name, archive_filename)
    
    # Security check: Ensure the path is within the archive directory
    if not os.path.abspath(file_path).startswith(os.path.abspath(CONTAINER_ARCHIVE_DIR)):
        flash('Invalid path specified.', 'danger')
        return redirect(url_for('archives_list'))

    try:
        os.remove(file_path)
        flash(f'Successfully deleted archive: {archive_filename}', 'success')
    except FileNotFoundError:
        flash('File not found. It may have already been deleted.', 'warning')
    except OSError as e:
        flash(f'Error deleting file: {e}', 'danger')
        
    return redirect(url_for('archives_list'))


if __name__ == '__main__':
    # For local development
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)



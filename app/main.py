import os
import psycopg2
from psycopg2.extras import DictCursor
import threading
import time
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, session, jsonify
from datetime import datetime
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

import backup

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- Configuration ---
DATABASE_URL = os.environ.get('DATABASE_URL')
LOCAL_STACKS_PATH = '/local'
CONTAINER_BACKUP_DIR = '/archives'  # Internal backup path inside the container

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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(100) PRIMARY KEY,
                value VARCHAR(255) NOT NULL
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
        # Set default retention if not present
        cur.execute("INSERT INTO settings (key, value) VALUES ('retention_days', '28') ON CONFLICT (key) DO NOTHING;")
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

def get_setting(key):
    """Gets a value from the settings table."""
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT value FROM settings WHERE key = %s;", (key,))
        result = cur.fetchone()
    conn.close()
    return result['value'] if result else None

def update_setting(key, value):
    """Updates a value in the settings table."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = %s;", (key, value, value))
        conn.commit()
    conn.close()

def discover_stacks():
    """Discovers Docker Compose stacks in the /local directory."""
    stacks = []
    if not os.path.isdir(LOCAL_STACKS_PATH):
        return stacks

    for volume_dir in os.listdir(LOCAL_STACKS_PATH):
        base_path = os.path.join(LOCAL_STACKS_PATH, volume_dir)
        if not os.path.isdir(base_path):
            continue
        
        for stack_dir in os.listdir(base_path):
            stack_path = os.path.join(base_path, stack_dir)
            if os.path.isdir(stack_path):
                # Check for a compose file to identify a stack
                if os.path.exists(os.path.join(stack_path, 'docker-compose.yml')) or \
                   os.path.exists(os.path.join(stack_path, 'compose.yaml')):
                    stacks.append({'name': stack_dir, 'path': stack_path})
    
    # Return sorted by stack name
    return sorted(stacks, key=lambda s: s['name'])


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
        
    return render_template('setup_initial_user.html')

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

        if user_data['email'] != new_email:
            update_user_email(user_id, new_email)
            flash('Email updated successfully!', 'success')
        
        if user_data['display_name'] != new_display_name:
            update_user_display_name(user_id, new_display_name)
            flash('Display name updated successfully!', 'success')
        
        if user_data['email'] == new_email and user_data['display_name'] == new_display_name:
            flash('No changes detected.', 'info')

        return redirect(url_for('profile_route'))
        
    return render_template('profile_edit.html', user=user_data)

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
    user_id = session['user_id']
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT * FROM users WHERE id = %s;", (user_id,))
        user = cur.fetchone()
    conn.close()

    if not user:
        return jsonify({"error": "User not found"}), 404
    
    user_passkeys = get_user_passkeys(user_id)

    options = generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=str(user['id']).encode('utf-8'),
        user_name=user['username'],
        exclude_credentials=[
            RegistrationCredential(id=key['credential_id']) for key in user_passkeys
        ],
    )

    session['webauthn_registration_challenge'] = base64.b64encode(options.challenge).decode('utf-8')
    return jsonify(options.dict())

@app.route('/webauthn/verify-registration', methods=['POST'])
def verify_registration_route():
    user_id = session['user_id']
    challenge = base64.b64decode(session.pop('webauthn_registration_challenge', None))
    
    if not user_id or not challenge:
        return jsonify({"error": "Missing session data"}), 400

    body = request.get_json()
    
    try:
        credential = RegistrationCredential.parse_raw(body)
        
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
            require_user_verification=True,
        )
    except Exception as e:
        return jsonify({"error": f"Registration failed: {e}"}), 400

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO passkeys (user_id, credential_id, public_key, sign_count, transports)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (
                user_id,
                verification.credential_id,
                verification.credential_public_key,
                verification.sign_count,
                ",".join(credential.response.transports or []),
            )
        )
        conn.commit()
    conn.close()

    return jsonify({"verified": True})

@app.route('/webauthn/generate-authentication-options', methods=['POST'])
def generate_authentication_options_route():
    username = request.get_json().get('username')
    if not username:
        # Discoverable credentials don't require a username upfront
        options = generate_authentication_options(rp_id=RP_ID)
        session['webauthn_authentication_challenge'] = base64.b64encode(options.challenge).decode('utf-8')
        return jsonify(options.dict())

    user = get_user(username)
    if not user:
        return jsonify({"error": "User not found"}), 404
        
    user_passkeys = get_user_passkeys(user['id'])
    if not user_passkeys:
        return jsonify({"error": "No passkeys registered for this user"}), 404

    options = generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=[
            {"id": key['credential_id'], "type": "public-key"} for key in user_passkeys
        ],
    )

    session['webauthn_authentication_challenge'] = base64.b64encode(options.challenge).decode('utf-8')
    return jsonify(options.dict())

@app.route('/webauthn/verify-authentication', methods=['POST'])
def verify_authentication_route():
    challenge = base64.b64decode(session.pop('webauthn_authentication_challenge', None))
    if not challenge:
        return jsonify({"error": "Missing session data"}), 400

    body = request.get_json()
    credential = AuthenticationCredential.parse_raw(body)
    
    credential_id = credential.raw_id

    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT * FROM passkeys WHERE credential_id = %s;", (credential_id,))
        db_passkey = cur.fetchone()

    if not db_passkey:
        return jsonify({"error": "Passkey not found"}), 404

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
            credential_public_key=db_passkey['public_key'],
            credential_current_sign_count=db_passkey['sign_count'],
            require_user_verification=True,
        )
    except Exception as e:
        return jsonify({"error": f"Authentication failed: {e}"}), 400

    # Update sign count to prevent replay attacks
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE passkeys SET sign_count = %s WHERE id = %s;",
            (verification.new_sign_count, db_passkey['id'])
        )
        conn.commit()
    conn.close()

    session['user_id'] = db_passkey['user_id']
    return jsonify({"verified": True})

@app.route('/webauthn/delete-passkey/<int:passkey_id>', methods=['POST'])
def delete_passkey_route(passkey_id):
    """Deletes a passkey for the logged-in user."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login_route'))

    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        # Ensure the passkey belongs to the logged-in user before deleting
        cur.execute("SELECT id FROM passkeys WHERE id = %s AND user_id = %s;", (passkey_id, user_id))
        passkey_to_delete = cur.fetchone()

        if passkey_to_delete:
            cur.execute("DELETE FROM passkeys WHERE id = %s;", (passkey_id,))
            conn.commit()
            flash('Passkey deleted successfully.', 'success')
        else:
            flash('Passkey not found or you do not have permission to delete it.', 'danger')
    
    conn.close()
    return redirect(url_for('profile_route'))


@app.route('/')
def index():
    """Main dashboard page."""
    stacks = discover_stacks()
    retention_days = get_setting('retention_days')
    
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        # Get last 10 jobs, including the SYSTEM_JOB entries
        cur.execute("SELECT * FROM archive_jobs ORDER BY start_time DESC LIMIT 10;")
        recent_jobs = cur.fetchall()
    conn.close()

    return render_template('index.html', stacks=stacks, retention_days=retention_days, recent_jobs=recent_jobs)

@app.route('/archive', methods=['POST'])
def start_archive_route():
    """Starts the archiving process in a background thread."""
    selected_stack_paths = request.form.getlist('stacks')
    retention_days = request.form.get('retention_days')

    if not selected_stack_paths:
        flash('Please select at least one stack to archive.', 'warning')
        return redirect(url_for('index'))

    update_setting('retention_days', retention_days)
    
    stack_names_for_flash = [os.path.basename(path) for path in selected_stack_paths]

    # Run the archiving process in a background thread
    thread = threading.Thread(
        target=backup.run_archive_job,
        args=(selected_stack_paths, retention_days, CONTAINER_BACKUP_DIR)
    )
    thread.start()

    flash(f"Archiving process started in the background for: {', '.join(stack_names_for_flash)}", 'info')
    return redirect(url_for('index'))


@app.route('/history')
def history():
    """Shows the full backup history."""
    conn = get_db_connection()
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute("SELECT * FROM archive_jobs ORDER BY start_time DESC;")
        all_jobs = cur.fetchall()
    conn.close()
    return render_template('history.html', jobs=all_jobs)


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
    archives_info = {}
    if not os.path.isdir(CONTAINER_BACKUP_DIR):
        return archives_info

    for stack_name in sorted(os.listdir(CONTAINER_BACKUP_DIR)):
        stack_dir_path = os.path.join(CONTAINER_BACKUP_DIR, stack_name)
        if os.path.isdir(stack_dir_path):
            archive_files = []
            total_size_bytes = 0
            last_backup_timestamp = 0

            for filename in sorted(os.listdir(stack_dir_path), reverse=True): # Sort to easily find last backup
                if filename.endswith(".tar"):
                    file_path = os.path.join(stack_dir_path, filename)
                    try:
                        stat_info = os.stat(file_path)
                        file_created_at = datetime.fromtimestamp(stat_info.st_mtime)
                        
                        archive_files.append({
                            "name": filename,
                            "size": stat_info.st_size,
                            "created_at": file_created_at
                        })
                        total_size_bytes += stat_info.st_size
                        if stat_info.st_mtime > last_backup_timestamp:
                            last_backup_timestamp = stat_info.st_mtime

                    except OSError:
                        # Skip files that might be gone due to race conditions
                        continue
            
            if archive_files:
                archives_info[stack_name] = {
                    "count": len(archive_files),
                    "total_size_bytes": total_size_bytes,
                    "total_size_human": format_bytes(total_size_bytes),
                    "last_backup": datetime.fromtimestamp(last_backup_timestamp) if last_backup_timestamp else None,
                    "files": archive_files
                }
    
    return archives_info


@app.route('/archives')
def archives_list():
    """Shows a list of available archives, grouped by stack."""
    all_archives = scan_archives()
    return render_template('archives.html', archives=all_archives)


@app.route('/download_archive/<path:stack_name>/<path:archive_filename>')
def download_archive(stack_name, archive_filename):
    """Provides a download for a specific archive file."""
    # Construct the directory path for the given stack
    stack_archive_dir = os.path.join(os.path.abspath(CONTAINER_BACKUP_DIR), stack_name)
    
    # Ensure the directory exists and is inside the main archive directory
    if not os.path.isdir(stack_archive_dir) or not stack_archive_dir.startswith(os.path.abspath(CONTAINER_BACKUP_DIR)):
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
    file_path = os.path.join(CONTAINER_BACKUP_DIR, stack_name, archive_filename)
    
    # Security check: Ensure the path is within the backup directory
    if not os.path.abspath(file_path).startswith(os.path.abspath(CONTAINER_BACKUP_DIR)):
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



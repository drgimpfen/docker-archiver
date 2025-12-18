"""
Main Flask application with Blueprints.
"""
import os
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from app.db import init_db, get_db
from app.auth import login_required, authenticate_user, create_user, get_user_count, get_current_user
from app.scheduler import init_scheduler, get_next_run_time
from app.stacks import discover_stacks
from app.downloads import get_download_by_token, prepare_archive_for_download
from app.notifications import get_setting
from app.utils import format_bytes, format_duration, get_disk_usage

# Import blueprints
from app.routes import archives, history, settings, profile


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Register blueprints
app.register_blueprint(archives.bp)
app.register_blueprint(history.bp)
app.register_blueprint(settings.bp)
app.register_blueprint(profile.bp)

# Initialize scheduler on startup
init_scheduler()


# Core routes
@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok'}), 200


@app.route('/')
@login_required
def index():
    """Dashboard page."""
    # Check maintenance mode
    maintenance_mode = get_setting('maintenance_mode', 'false').lower() == 'true'
    
    # Get disk usage
    disk = get_disk_usage()
    
    # Get all archives
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM archives ORDER BY name;")
        archives_list = cur.fetchall()
        
        # Enrich with stats
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
            SELECT j.*, a.name as archive_name 
            FROM jobs j
            LEFT JOIN archives a ON j.archive_id = a.id
            ORDER BY j.start_time DESC
            LIMIT 10;
        """)
        recent_jobs = cur.fetchall()
    
    return render_template(
        'index.html',
        archives=archive_list,
        recent_jobs=recent_jobs,
        disk=disk,
        maintenance_mode=maintenance_mode,
        format_bytes=format_bytes,
        format_duration=format_duration,
        current_user=get_current_user()
    )


@app.route('/login', methods=['GET', 'POST'])
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
    download_info = get_download_by_token(token)
    
    if not download_info:
        return "Invalid or expired download link", 404
    
    file_path = download_info['file_path']
    
    # Prepare archive (create if folder)
    actual_path, should_cleanup = prepare_archive_for_download(file_path, output_format='tar.gz')
    
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

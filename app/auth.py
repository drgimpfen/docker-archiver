"""
User authentication and session management.
"""
import bcrypt
from functools import wraps
from flask import session, redirect, url_for, flash
from app.db import get_db


def hash_password(password):
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password, password_hash):
    """Verify a password against a hash."""
    return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))


def create_user(username, password, email=None, role='admin'):
    """Create a new user and set created_at explicitly using UTC-aware utils.now()."""
    password_hash = hash_password(password)
    from app import utils as _utils
    created_at = _utils.now()
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO users (username, password_hash, email, role, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id;",
                (username, password_hash, email, role, created_at)
            )
            user_id = cur.fetchone()['id']
            conn.commit()
            return user_id
        except Exception as e:
            conn.rollback()
            raise e


def authenticate_user(username, password):
    """Authenticate a user and return user info if successful."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = %s;", (username,))
        user = cur.fetchone()
        
        if user and verify_password(password, user['password_hash']):
            # Update last login using tz-aware UTC timestamp
            from app import utils as _utils
            now_ts = _utils.now()
            cur.execute("UPDATE users SET last_login = %s WHERE id = %s RETURNING id, username, email, role, created_at, last_login;", (now_ts, user['id']))
            conn.commit()
            return cur.fetchone()
        return None


def get_user_count():
    """Get total number of users."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as count FROM users;")
        return cur.fetchone()['count']


def login_required(f):
    """Decorator to require login for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def get_current_user():
    """Get current logged-in user info."""
    if 'user_id' not in session:
        return None
    
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, email, role, created_at, last_login FROM users WHERE id = %s;", (session['user_id'],))
        return cur.fetchone()

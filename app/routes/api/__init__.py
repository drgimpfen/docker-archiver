"""
API package initializer.
Provides shared Blueprint `bp` and `api_auth_required` decorator used across submodules.
"""
from functools import wraps
from flask import Blueprint, request, jsonify
from app.db import get_db
from app.auth import login_required

bp = Blueprint('api', __name__, url_prefix='/api')


def api_auth_required(f):
    """
    Decorator for API endpoints that accepts both session auth and API token auth.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check for API token in Authorization header
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header[7:]  # Remove 'Bearer ' prefix

            # Validate token
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT u.* FROM users u
                    JOIN api_tokens t ON u.id = t.user_id
                    WHERE t.token = %s AND (t.expires_at IS NULL OR t.expires_at > NOW());
                """, (token,))
                user = cur.fetchone()

            if user:
                # Token is valid, proceed
                return f(*args, **kwargs)
            else:
                return jsonify({'error': 'Invalid or expired API token'}), 401

        # Fall back to session auth (for web UI)
        return login_required(f)(*args, **kwargs)

    return decorated_function


# Import submodules to ensure blueprint route registration on package import
# Submodules should import `bp` and `api_auth_required` from this package
try:
    from app.routes.api import jobs, cleanup, downloads, sse  # noqa: F401
except Exception:
    # Import errors will surface during tests/import time if something is wrong
    pass

__all__ = ["bp", "api_auth_required"]

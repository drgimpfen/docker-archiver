"""
User profile routes.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from app.auth import login_required, get_current_user, hash_password, verify_password
from app.db import get_db


bp = Blueprint('profile', __name__, url_prefix='/profile')


@bp.route('/', methods=['GET', 'POST'])
@login_required
def manage_profile():
    """User profile page."""
    user = get_current_user()
    
    if request.method == 'POST':
        try:
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip()
            current_password = request.form.get('current_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')
            
            if not username:
                flash('Username is required', 'danger')
                return redirect(url_for('profile.manage_profile'))
            
            with get_db() as conn:
                cur = conn.cursor()
                
                # Check if username is already taken by another user
                if username != user['username']:
                    cur.execute("SELECT id FROM users WHERE username = %s AND id != %s;", (username, user['id']))
                    if cur.fetchone():
                        flash('Username already taken', 'danger')
                        return redirect(url_for('profile.manage_profile'))
                
                # Update username and email
                cur.execute("""
                    UPDATE users 
                    SET username = %s, email = %s, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = %s;
                """, (username, email, user['id']))
                
                # Update password if provided
                if new_password:
                    if not current_password:
                        flash('Current password is required to change password', 'danger')
                        conn.rollback()
                        return redirect(url_for('profile.manage_profile'))
                    
                    # Verify current password
                    if not verify_password(current_password, user['password_hash']):
                        flash('Current password is incorrect', 'danger')
                        conn.rollback()
                        return redirect(url_for('profile.manage_profile'))
                    
                    if new_password != confirm_password:
                        flash('New passwords do not match', 'danger')
                        conn.rollback()
                        return redirect(url_for('profile.manage_profile'))
                    
                    if len(new_password) < 6:
                        flash('Password must be at least 6 characters', 'danger')
                        conn.rollback()
                        return redirect(url_for('profile.manage_profile'))
                    
                    # Update password
                    password_hash = hash_password(new_password)
                    cur.execute("""
                        UPDATE users 
                        SET password_hash = %s, updated_at = CURRENT_TIMESTAMP 
                        WHERE id = %s;
                    """, (password_hash, user['id']))
                
                conn.commit()
                
                # Update session username if changed
                if username != user['username']:
                    session['username'] = username
                
                flash('Profile updated successfully!', 'success')
                return redirect(url_for('profile.manage_profile'))
                
        except Exception as e:
            flash(f'Error updating profile: {e}', 'danger')
    
    return render_template(
        'profile.html',
        current_user=get_current_user()
    )

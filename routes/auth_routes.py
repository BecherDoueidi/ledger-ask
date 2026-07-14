from flask import Blueprint, request, render_template, redirect, url_for, session

import auth

auth_bp = Blueprint("auth", __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login form (GET) and credential check (POST)."""
    if request.method == 'GET':
        if "username" in session:
            return redirect(url_for('pages.index'))
        return render_template('login.html', error=None)

    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    user = auth.verify_user(username, password)
    if user is None:
        return render_template('login.html', error="Incorrect username or password."), 401

    session['username'] = user['username']
    session['role'] = user['role']
    session['donor_id'] = user['donor_id']
    return redirect(url_for('pages.index'))


@auth_bp.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('auth.login'))

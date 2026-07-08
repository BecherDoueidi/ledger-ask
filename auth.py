"""
User store for login-based authentication.

Previously, role ("admin"/"donor") and donor_id were sent directly by
the CLIENT in the request body to /api/generate-sql. That meant any
visitor could open devtools and POST {"role": "admin"} to get
unrestricted database access -- the entire row-level-filter and
table-access defense in access_control.py was moot if the caller could
simply claim to be an admin.

This module replaces that with real login: a user authenticates once
with a username + password, we verify the password hash, and the
resulting role/donor_id are stored server-side in the Flask session
(see app.py). The client can no longer choose its own role.

SQLite-backed (users.db) so accounts persist across restarts, with
passwords stored as salted hashes (never plaintext) via werkzeug's
generate_password_hash/check_password_hash -- the same hashing helpers
Flask itself depends on, so no new dependency is introduced.
"""

import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash

USERS_DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")


def _get_connection():
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username      TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL,
            donor_id      INTEGER
        )
        """
    )
    return conn


def create_user(username, password, role, donor_id=None):
    """
    Creates a new user. Raises sqlite3.IntegrityError if the username
    already exists. Password is hashed before storage -- it is never
    kept in plaintext anywhere, including in memory after this call.
    """
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, donor_id) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), role, donor_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_user(username):
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT username, password_hash, role, donor_id FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            return None
        return {"username": row[0], "password_hash": row[1], "role": row[2], "donor_id": row[3]}
    finally:
        conn.close()


def verify_user(username, password):
    """
    Returns the user dict (without password_hash) if username/password
    match, or None otherwise. Deliberately returns None for both
    "user doesn't exist" and "wrong password" -- distinguishing the two
    to a caller lets an attacker enumerate valid usernames.
    """
    user = get_user(username)
    if user is None:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return {"username": user["username"], "role": user["role"], "donor_id": user["donor_id"]}


def list_users():
    """Debug/seed helper -- returns all usernames and roles, no hashes."""
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT username, role, donor_id FROM users").fetchall()
        return [{"username": r[0], "role": r[1], "donor_id": r[2]} for r in rows]
    finally:
        conn.close()


def seed_default_users():
    """
    Called once at app startup. Only ever creates accounts that don't
    already exist, so it's safe to call on every boot -- it will never
    overwrite a password an admin has already changed.

    Ships with one account per role tier (viewer/analyst/admin/donor) so
    every capability boundary in roles_config.py is exercisable out of
    the box without an admin having to provision test accounts first.
    CHANGE THESE PASSWORDS (or delete users.db and create your own via
    create_user) before deploying anywhere real -- shipping known
    default credentials is itself a vulnerability.
    """
    defaults = [
        ("admin", "admin123", "admin", None),
        ("analyst1", "analyst123", "analyst", None),
        ("viewer1", "viewer123", "viewer", None),
        ("donor1", "donor123", "donor", 1),
    ]
    for username, password, role, donor_id in defaults:
        if get_user(username) is None:
            create_user(username, password, role, donor_id)

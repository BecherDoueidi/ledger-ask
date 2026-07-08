"""
auth.py -- login store. verify_user's "return None for both unknown
user and wrong password" behavior is a deliberate anti-enumeration
choice documented in the module; tested explicitly since it's easy to
accidentally regress by "helpfully" distinguishing the two errors.
"""

import sqlite3

import pytest

import auth


def test_create_and_verify_user():
    auth.create_user("alice", "secret123", "admin", None)
    user = auth.verify_user("alice", "secret123")
    assert user == {"username": "alice", "role": "admin", "donor_id": None}


def test_password_is_hashed_not_plaintext():
    auth.create_user("alice", "secret123", "admin", None)
    stored = auth.get_user("alice")
    assert stored["password_hash"] != "secret123"


def test_verify_user_wrong_password_returns_none():
    auth.create_user("alice", "secret123", "admin", None)
    assert auth.verify_user("alice", "wrong-password") is None


def test_verify_user_unknown_username_returns_none():
    assert auth.verify_user("nobody", "whatever") is None


def test_duplicate_username_raises_integrity_error():
    auth.create_user("alice", "pw1", "admin", None)
    with pytest.raises(sqlite3.IntegrityError):
        auth.create_user("alice", "pw2", "donor", 1)


def test_donor_user_carries_donor_id():
    auth.create_user("donor1", "pw", "donor", 42)
    user = auth.verify_user("donor1", "pw")
    assert user["donor_id"] == 42


def test_list_users_never_exposes_password_hash():
    auth.create_user("alice", "secret123", "admin", None)
    users = auth.list_users()
    assert all("password_hash" not in u for u in users)
    assert users == [{"username": "alice", "role": "admin", "donor_id": None}]


def test_seed_default_users_creates_admin_and_donor1():
    auth.seed_default_users()
    assert auth.verify_user("admin", "admin123") == {"username": "admin", "role": "admin", "donor_id": None}
    assert auth.verify_user("donor1", "donor123") == {"username": "donor1", "role": "donor", "donor_id": 1}


def test_seed_default_users_does_not_overwrite_changed_password():
    auth.seed_default_users()
    # Simulate an admin who already changed their password: re-seeding
    # must be a no-op for an existing username, never resetting it back.
    conn = sqlite3.connect(auth.USERS_DB_PATH)
    from werkzeug.security import generate_password_hash
    conn.execute("UPDATE users SET password_hash = ? WHERE username = 'admin'",
                 (generate_password_hash("new-password"),))
    conn.commit()
    conn.close()

    auth.seed_default_users()
    assert auth.verify_user("admin", "new-password") is not None
    assert auth.verify_user("admin", "admin123") is None

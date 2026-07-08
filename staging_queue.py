"""
Staging queue: every NEW question that goes through the full LLM
pipeline gets logged here so it shows up in the admin review screen
(admin.html -> GET /api/queue). An admin can then "promote" a good
one, which moves it into catalog.yaml as a permanent, LLM-free
shortcut (see catalog_manager.py).

Cache hits and catalog hits are NOT logged here -- they've already
been reviewed once (or are a deliberately curated catalog entry), so
re-logging them every time they're asked again would just clutter the
queue with duplicates.
"""

import sqlite3
import os
from datetime import datetime, timezone

STAGING_DB_PATH = os.path.join(os.path.dirname(__file__), "staging_queue.db")


def _get_connection():
    conn = sqlite3.connect(STAGING_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS staging_queue (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            question   TEXT NOT NULL,
            role_name  TEXT,
            donor_id   INTEGER,
            sql        TEXT,
            status     TEXT NOT NULL,
            details    TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    # Backfill for DBs created before role_name/donor_id existed.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(staging_queue)")}
    if "role_name" not in existing_cols:
        conn.execute("ALTER TABLE staging_queue ADD COLUMN role_name TEXT")
    if "donor_id" not in existing_cols:
        conn.execute("ALTER TABLE staging_queue ADD COLUMN donor_id INTEGER")
    return conn


def log_entry(question, role_name, donor_id, sql, status, details=""):
    """
    question is the RAW question text the user typed -- never prefixed
    with role/donor info. Who asked it is tracked in its own columns
    (role_name, donor_id) so it can never leak into a promoted catalog
    intent (see catalog_manager.promote / app.py promote_entry).

    status is one of: 'Approved', 'Rejected', 'Blocked'.
    Returns the new row's id.
    """
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO staging_queue (question, role_name, donor_id, sql, status, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (question, role_name, donor_id, sql, status, details, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_queue():
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT id, question, role_name, donor_id, sql, status, details FROM staging_queue "
            "ORDER BY id DESC"
        ).fetchall()
        return [
            {
                "id": r[0], "question": r[1], "role_name": r[2], "donor_id": r[3],
                "sql": r[4], "status": r[5], "details": r[6],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_entry(entry_id):
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id, question, role_name, donor_id, sql, status, details FROM staging_queue WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "question": row[1], "role_name": row[2], "donor_id": row[3],
            "sql": row[4], "status": row[5], "details": row[6],
        }
    finally:
        conn.close()


def mark_promoted(entry_id):
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE staging_queue SET status = 'Promoted' WHERE id = ?", (entry_id,)
        )
        conn.commit()
    finally:
        conn.close()

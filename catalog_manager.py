"""
The "Path 3: Deterministic Compiler" bypass described in the README.
Questions whose exact wording matches a promoted, ACTIVE catalog entry
skip the LLM entirely: the stored SQL is run live against the database
(so the data is always fresh) with zero model inference cost.

SQLite-backed, not a flat catalog.yaml file (which this replaces): every
promotion is versioned and starts life as 'pending', not live traffic.
Promoting a query (see routes/admin_api.py's promote_entry, called from
the staging-queue review UI) only stages it -- it has zero effect on
what any user's question matches until a second, explicit approval step
(approve_entry) flips it to 'active'. This is a real two-person-style
control for a shortcut that, once active, answers every future user's
matching question with zero further review: one admin promoting their
own staging-queue entry is not the same guarantee as a second look
before it goes live.

Versioning: promoting the same intent text again (e.g. a corrected SQL
for a previously-promoted question) creates a new row with an
incremented version rather than overwriting anything. Approving it
supersedes whichever version was previously active for that intent, so
there's always a full history of what was live when, not just the
current state.
"""

import os
import sqlite3
from datetime import datetime, timezone

CATALOG_DB_PATH = os.path.join(os.path.dirname(__file__), "catalog.db")

# Only imported from if catalog.yaml exists on the very first run, to
# carry over any pre-existing promoted entries into the new store
# instead of silently discarding them. Not needed once migrated (see
# _migrate_legacy_yaml_if_needed).
_LEGACY_YAML_PATH = os.path.join(os.path.dirname(__file__), "catalog.yaml")


def _normalize(text_value):
    return " ".join(text_value.strip().lower().split())


def _get_connection():
    conn = sqlite3.connect(CATALOG_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_entries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            intent        TEXT NOT NULL,
            intent_norm   TEXT NOT NULL,
            sql           TEXT NOT NULL,
            version       INTEGER NOT NULL,
            status        TEXT NOT NULL,   -- 'pending' | 'active' | 'rejected' | 'superseded'
            promoted_by   TEXT,
            promoted_at   TEXT NOT NULL,
            approved_by   TEXT,
            approved_at   TEXT,
            notes         TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_intent_norm ON catalog_entries(intent_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_status ON catalog_entries(status)")
    conn.commit()
    _migrate_legacy_yaml_if_needed(conn)
    return conn


def _migrate_legacy_yaml_if_needed(conn):
    """
    One-time carry-over from the old catalog.yaml file: if the new table
    is completely empty AND a legacy YAML file exists, import each of
    its entries as an already-active, version-1 row -- so upgrading to
    this module never silently drops a shortcut that was already live.
    Safe to call on every connection: it's a no-op the moment the table
    has any rows at all, migrated or not.
    """
    if not os.path.exists(_LEGACY_YAML_PATH):
        return
    existing = conn.execute("SELECT COUNT(*) FROM catalog_entries").fetchone()[0]
    if existing > 0:
        return

    import yaml
    with open(_LEGACY_YAML_PATH, "r") as f:
        data = yaml.safe_load(f) or {}
    entries = data.get("promoted_queries") or []
    if not entries:
        return

    now = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        intent, sql = entry.get("intent"), entry.get("sql")
        if not intent or not sql:
            continue
        conn.execute(
            """
            INSERT INTO catalog_entries
                (intent, intent_norm, sql, version, status, promoted_by, promoted_at, approved_by, approved_at, notes)
            VALUES (?, ?, ?, 1, 'active', 'migration', ?, 'migration', ?, 'Migrated from legacy catalog.yaml')
            """,
            (intent, _normalize(intent), sql, now, now),
        )
    conn.commit()


def find_match(user_query):
    """
    Returns the ACTIVE promoted SQL string for this question
    (case/whitespace-insensitive), or None. Pending, rejected, and
    superseded entries are never matched -- only an approved, currently
    active version can short-circuit the LLM for live traffic.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT sql FROM catalog_entries WHERE intent_norm = ? AND status = 'active'",
            (_normalize(user_query),),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def promote(intent, sql, promoted_by=None):
    """
    Stages a new catalog entry as 'pending' -- this has NO effect on
    find_match() until approve_entry() is called separately. Version
    number is one higher than the highest existing version for this
    exact (normalized) intent, so re-promoting a corrected SQL for a
    previously-promoted question is tracked as history, not an
    overwrite.

    `intent` must be the raw question text with no role/donor prefix
    baked in, and `sql` must not depend on any one person's row-level
    scope -- the catalog is a GLOBAL, unrestricted shortcut. That's why
    routes/admin_api.py's promote_entry only allows promoting entries
    that were originally asked by an unrestricted (non-row-filtered)
    role in the first place.

    Returns the new entry's id.
    """
    conn = _get_connection()
    try:
        intent_norm = _normalize(intent)
        max_version = conn.execute(
            "SELECT MAX(version) FROM catalog_entries WHERE intent_norm = ?", (intent_norm,)
        ).fetchone()[0]
        next_version = (max_version or 0) + 1

        cursor = conn.execute(
            """
            INSERT INTO catalog_entries
                (intent, intent_norm, sql, version, status, promoted_by, promoted_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (intent, intent_norm, sql, next_version, promoted_by, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def approve_entry(entry_id, approved_by=None):
    """
    Activates a pending entry: it becomes what find_match() returns for
    its intent, and any PREVIOUSLY active entry for that same intent is
    marked 'superseded' (not deleted -- still visible via list_entries
    for audit history). Returns False if entry_id doesn't exist or
    isn't currently pending (e.g. already approved, or rejected).
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT intent_norm, status FROM catalog_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None or row[1] != "pending":
            return False
        intent_norm = row[0]
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "UPDATE catalog_entries SET status = 'superseded' WHERE intent_norm = ? AND status = 'active'",
            (intent_norm,),
        )
        conn.execute(
            "UPDATE catalog_entries SET status = 'active', approved_by = ?, approved_at = ? WHERE id = ?",
            (approved_by, now, entry_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def reject_entry(entry_id, reason=None):
    """Marks a pending entry as rejected. Returns False if it isn't currently pending."""
    conn = _get_connection()
    try:
        row = conn.execute("SELECT status FROM catalog_entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None or row[0] != "pending":
            return False
        conn.execute(
            "UPDATE catalog_entries SET status = 'rejected', notes = ? WHERE id = ?",
            (reason, entry_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def list_entries(status=None):
    """All catalog entries (any status), newest first -- powers the admin catalog review UI."""
    conn = _get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT id, intent, sql, version, status, promoted_by, promoted_at, approved_by, approved_at, notes "
                "FROM catalog_entries WHERE status = ? ORDER BY id DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, intent, sql, version, status, promoted_by, promoted_at, approved_by, approved_at, notes "
                "FROM catalog_entries ORDER BY id DESC"
            ).fetchall()
        cols = ["id", "intent", "sql", "version", "status", "promoted_by", "promoted_at", "approved_by", "approved_at", "notes"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def get_entry(entry_id):
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id, intent, sql, version, status, promoted_by, promoted_at, approved_by, approved_at, notes "
            "FROM catalog_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
        cols = ["id", "intent", "sql", "version", "status", "promoted_by", "promoted_at", "approved_by", "approved_at", "notes"]
        return dict(zip(cols, row))
    finally:
        conn.close()

"""
Per-session conversation state for multi-turn follow-up queries (see
followup_resolver.py for the classification/transform logic that uses
this, and app.py for where it's wired into the request pipeline).

Storage: SQLite (conversation_state.db), matching every other piece of
state in this app (query_cache.db, query_analytics.db, staging_queue.db,
users.db) rather than introducing an in-memory dict or a new dependency
like Redis -- this is a single small table, it needs to survive a
server restart just like everything else here, and a second storage
technology would be inconsistent complexity for no real benefit at this
project's scale.

Keying: one row per conversation_id, a random token minted into the
Flask session on first use (see app.py) -- NOT per role/donor_id alone,
because a single logged-in user might want to hold two independent
conversations in two browser tabs. role_name/donor_id are still stored
and re-checked on every read (see get_state) so a stale conversation
can never be replayed under different access-control scoping, exactly
like query_cache.py's re-validation of cached SQL against current
allowed_tables.

TTL: a conversation older than CONVERSATION_TTL_MINUTES is treated as
if it doesn't exist. Without this, "now show total donations" typed
three hours after the original question would silently attach to a
long-stale table -- a fresh topic that happens to use a referential
word shouldn't accidentally inherit ancient context.
"""

import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta

CONVERSATION_DB_PATH = os.path.join(os.path.dirname(__file__), "conversation_state.db")
CONVERSATION_TTL_MINUTES = 30

# Cap on how many rows of the previous result we persist/re-serve for
# transform purposes. This app's questions return dozens to low hundreds
# of rows in practice; a follow-up chain operating on more than this many
# rows gets better served by a fresh SQL-level LIMIT/aggregate anyway (see
# followup_resolver.py), so there's no reason to carry a huge blob through
# every turn of a conversation.
MAX_STORED_ROWS = 2000


def _get_connection():
    conn = sqlite3.connect(CONVERSATION_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_state (
            conversation_id TEXT PRIMARY KEY,
            role_name       TEXT NOT NULL,
            donor_id        INTEGER,
            last_question   TEXT NOT NULL,
            last_sql        TEXT NOT NULL,
            last_rows_json  TEXT NOT NULL,
            last_visualization_json TEXT,
            transform_log_json TEXT NOT NULL DEFAULT '[]',
            updated_at      TEXT NOT NULL
        )
        """
    )
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversation_state)")}
    if "transform_log_json" not in existing_cols:
        conn.execute("ALTER TABLE conversation_state ADD COLUMN transform_log_json TEXT NOT NULL DEFAULT '[]'")
    return conn


def get_state(conversation_id, role_name, donor_id):
    """
    Returns {"last_question", "last_sql", "rows", "visualization",
    "transform_log"} if a live (non-expired, same role+donor) conversation
    exists, else None. transform_log is the list of plain-English
    descriptions of every deterministic transform applied on top of
    last_sql since it last actually ran against the database (see
    followup_resolver.describe_transform) -- it's what lets the UI show
    an honest "what you're looking at" trail without needing to
    reconstruct a nested SQL string for operations that don't map onto
    one cleanly (e.g. dedupe-by-column).
    """
    if not conversation_id:
        return None
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT role_name, donor_id, last_question, last_sql,
                   last_rows_json, last_visualization_json, transform_log_json, updated_at
            FROM conversation_state WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None

        (stored_role, stored_donor_id, last_question, last_sql, rows_json,
         viz_json, transform_log_json, updated_at) = row

        # Different role/donor than who wrote this state -- e.g. a logout
        # and login as someone else re-using the same browser session
        # object before a fresh conversation_id was minted. Never carry
        # one identity's context into another's turn.
        if stored_role != role_name or stored_donor_id != donor_id:
            return None

        updated = datetime.fromisoformat(updated_at)
        if datetime.now(timezone.utc) - updated > timedelta(minutes=CONVERSATION_TTL_MINUTES):
            return None

        return {
            "last_question": last_question,
            "last_sql": last_sql,
            "rows": json.loads(rows_json),
            "visualization": json.loads(viz_json) if viz_json else None,
            "transform_log": json.loads(transform_log_json) if transform_log_json else [],
        }
    finally:
        conn.close()


def save_state(conversation_id, role_name, donor_id, question, sql, rows, visualization, transform_log=None):
    """
    Overwrites this conversation's state with the new "current" turn.
    transform_log defaults to [] -- pass the previous log only when
    appending another deterministic transform on top of the SAME last_sql
    (see app.py); a fresh question or an LLM-modified query always resets
    it to [], since sql itself changed and the log would otherwise
    describe transforms that no longer apply to what's being shown.
    """
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO conversation_state
                (conversation_id, role_name, donor_id, last_question, last_sql,
                 last_rows_json, last_visualization_json, transform_log_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                role_name               = excluded.role_name,
                donor_id                = excluded.donor_id,
                last_question           = excluded.last_question,
                last_sql                = excluded.last_sql,
                last_rows_json          = excluded.last_rows_json,
                last_visualization_json = excluded.last_visualization_json,
                transform_log_json      = excluded.transform_log_json,
                updated_at              = excluded.updated_at
            """,
            (
                conversation_id, role_name, donor_id, question, sql,
                json.dumps((rows or [])[:MAX_STORED_ROWS], default=str),
                json.dumps(visualization) if visualization else None,
                json.dumps(transform_log or []),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def clear_state(conversation_id):
    """Explicit reset -- powers the "New question" control in the UI."""
    if not conversation_id:
        return
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM conversation_state WHERE conversation_id = ?", (conversation_id,))
        conn.commit()
    finally:
        conn.close()

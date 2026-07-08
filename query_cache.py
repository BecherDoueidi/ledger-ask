"""
Persistent question/answer cache.

Goal: if a user asks the same question twice (now or after a server
restart), the second time should skip the LLM call and the DB query
entirely and just return the saved answer instantly. This has grown to
cover not just literal repeats but *semantically equivalent* rephrasings
("Show all employees" vs "Can you list every employee?") -- see the
"Matching" section below.

Storage: a small SQLite file (query_cache.db) sitting next to this script.
SQLite is used (rather than an in-memory dict) specifically because it
survives process restarts -- nothing is lost when the server reboots.

Scoping: every entry is keyed on (role_name, donor_id, normalized
question) -- NOT on question text alone. This means a donor's cache and
an admin's cache are physically separate rows, and two different donors
never share an entry either. role_name/donor_id are real columns (not
string-concatenated into the question) so they can be inspected,
audited, and enforced without relying on text parsing. Semantic lookups
(below) are scanned within this exact same (role_name, donor_id)
partition -- semantic matching never widens who a cached answer is
visible to, it only widens which *wording* can reach an existing answer.

Matching -- two layers, cheapest first:
1. EXACT: the question text is normalized (lowercased, whitespace
   collapsed) and hashed together with role_name/donor_id. This is an
   O(1) lookup and 100% precise; it always runs first and never touches
   the embedding model.
2. SEMANTIC (only tried on an exact-match miss): the incoming question is
   embedded locally (see embeddings.py) and compared via cosine
   similarity against every other cached entry in this same role+donor
   partition (see semantic_match.py). A match is only trusted if BOTH
   the similarity score clears a high threshold AND both questions agree
   on every number/date/quarter/relative-time word found in either one
   (the "entity guard") -- this is what stops "top 5 donors" from
   matching a cached "top 10 donors" answer, since those embed close
   together but are not the same question.
   If embeddings are unavailable (no embedding-capable model pulled on
   the local Ollama daemon, connection error, etc.) this layer is
   silently skipped and the cache behaves exactly as it did before --
   exact-match only. See embeddings.py for that fallback.

IMPORTANT: a cache hit (exact OR semantic) is a shortcut around the LLM +
SQL generation, but it must NEVER be a shortcut around access control.
The caller (app.py) is responsible for re-validating a cached entry's SQL
against the CURRENT role's allowed_tables (access_control.check_table_access)
before returning it, and for calling invalidate_by_key() if that check
fails -- otherwise a permissions change (or a bug that let one bad entry
through) would keep being replayed forever.
"""

import sqlite3
import hashlib
import json
import os
from datetime import datetime, timezone

import embeddings
import semantic_match

CACHE_DB_PATH = os.path.join(os.path.dirname(__file__), "query_cache.db")


def _get_connection():
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS query_cache (
            cache_key      TEXT PRIMARY KEY,
            role_name      TEXT NOT NULL,
            donor_id       INTEGER,
            original_query TEXT NOT NULL,
            generated_sql  TEXT NOT NULL,
            result_json    TEXT NOT NULL,
            hit_count      INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL,
            last_used_at   TEXT NOT NULL
        )
        """
    )
    # Backfill for DBs created before role_name/donor_id existed, back
    # when the primary key was just a hash of the raw question text.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(query_cache)")}
    if "role_name" not in existing_cols:
        # Old rows have no reliable role/donor association -- wipe them
        # rather than guessing, since guessing wrong here means leaking
        # data across roles, which is exactly the bug being fixed.
        conn.execute("DROP TABLE query_cache")
        conn.execute(
            """
            CREATE TABLE query_cache (
                cache_key      TEXT PRIMARY KEY,
                role_name      TEXT NOT NULL,
                donor_id       INTEGER,
                original_query TEXT NOT NULL,
                generated_sql  TEXT NOT NULL,
                result_json    TEXT NOT NULL,
                hit_count      INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT NOT NULL,
                last_used_at   TEXT NOT NULL
            )
            """
        )
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(query_cache)")}

    # Additive backfill for DBs created before semantic matching existed.
    # NULL embedding/entity_signature/content_words on old rows just means
    # those rows never participate in a semantic match until they're
    # re-written by set_cached -- they remain perfectly valid exact-match
    # entries.
    if "embedding" not in existing_cols:
        conn.execute("ALTER TABLE query_cache ADD COLUMN embedding TEXT")
    if "entity_signature" not in existing_cols:
        conn.execute("ALTER TABLE query_cache ADD COLUMN entity_signature TEXT")
    if "content_words" not in existing_cols:
        conn.execute("ALTER TABLE query_cache ADD COLUMN content_words TEXT")

    return conn


def _normalize(query_text):
    return " ".join(query_text.strip().lower().split())


def _cache_key(query_text, role_name, donor_id):
    raw = f"{role_name}|{donor_id}|{_normalize(query_text)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_to_result(cache_key, generated_sql, result_json, hit_count, match_type, similarity=1.0):
    return {
        "cache_key": cache_key,
        "generated_sql": generated_sql,
        "data": json.loads(result_json),
        "hit_count": hit_count,
        "match_type": match_type,
        "similarity": round(similarity, 4),
    }


def _touch_hit_count(conn, cache_key, current_hit_count):
    new_hit_count = current_hit_count + 1
    conn.execute(
        "UPDATE query_cache SET hit_count = ?, last_used_at = ? WHERE cache_key = ?",
        (new_hit_count, datetime.now(timezone.utc).isoformat(), cache_key),
    )
    conn.commit()
    return new_hit_count


def get_cached(query_text, role_name, donor_id):
    """
    Exact-match lookup only (see module docstring). Returns a dict with
    generated_sql / data / hit_count / cache_key / match_type="exact", or
    None if this literal question (after normalization) has never been
    asked before by this same role+donor.
    """
    cache_key = _cache_key(query_text, role_name, donor_id)
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT generated_sql, result_json, hit_count FROM query_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        generated_sql, result_json, hit_count = row
        new_hit_count = _touch_hit_count(conn, cache_key, hit_count)
        return _row_to_result(cache_key, generated_sql, result_json, new_hit_count, "exact")
    finally:
        conn.close()


def find_semantic_match(query_text, role_name, donor_id):
    """
    Only meaningful to call after get_cached() has already missed. Embeds
    query_text and compares it against every OTHER cached entry in this
    same role+donor partition. Returns the same shape as get_cached()
    (with match_type="semantic" and a similarity score), or None if
    embeddings are unavailable or nothing qualifies.
    """
    query_embedding = embeddings.get_embedding(query_text)
    if query_embedding is None:
        return None

    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT cache_key, generated_sql, result_json, hit_count, embedding,
                   entity_signature, content_words
            FROM query_cache
            WHERE role_name = ? AND donor_id IS ? AND embedding IS NOT NULL
            """,
            (role_name, donor_id),
        ).fetchall()

        candidates = []
        for cache_key, generated_sql, result_json, hit_count, embedding_json, signature_json, content_words_json in rows:
            candidates.append({
                "cache_key": cache_key,
                "generated_sql": generated_sql,
                "result_json": result_json,
                "hit_count": hit_count,
                "embedding": json.loads(embedding_json),
                "entity_signature": frozenset(json.loads(signature_json)),
                "content_words": frozenset(json.loads(content_words_json)) if content_words_json else frozenset(),
            })

        match, score = semantic_match.find_best_match(query_text, query_embedding, candidates)
        if match is None:
            return None

        new_hit_count = _touch_hit_count(conn, match["cache_key"], match["hit_count"])
        return _row_to_result(
            match["cache_key"], match["generated_sql"], match["result_json"],
            new_hit_count, "semantic", similarity=score,
        )
    finally:
        conn.close()


def get_cached_or_similar(query_text, role_name, donor_id):
    """
    The lookup app.py should actually call: exact-match first (free,
    100% precise), then semantic match as a fallback (costs one local
    embedding call, only on an exact-match miss). See module docstring.
    """
    exact = get_cached(query_text, role_name, donor_id)
    if exact is not None:
        return exact
    return find_semantic_match(query_text, role_name, donor_id)


def set_cached(query_text, role_name, donor_id, generated_sql, data):
    """
    Save a new question -> answer pair so future repeats (exact or
    semantic, by this same role+donor) are instant. Embedding generation
    failure is non-fatal: the row is still written with embedding=NULL,
    which simply means it won't be considered for future semantic
    matches until it's re-written (exact-match caching is unaffected).
    """
    cache_key = _cache_key(query_text, role_name, donor_id)
    now = datetime.now(timezone.utc).isoformat()

    embedding = embeddings.get_embedding(query_text)
    embedding_json = json.dumps(embedding) if embedding is not None else None
    entity_signature_json = json.dumps(sorted(semantic_match.extract_entity_signature(query_text)))
    content_words_json = json.dumps(sorted(semantic_match.extract_content_words(query_text)))

    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO query_cache
                (cache_key, role_name, donor_id, original_query, generated_sql,
                 result_json, hit_count, created_at, last_used_at, embedding,
                 entity_signature, content_words)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                generated_sql    = excluded.generated_sql,
                result_json      = excluded.result_json,
                last_used_at     = excluded.last_used_at,
                embedding        = excluded.embedding,
                entity_signature = excluded.entity_signature,
                content_words    = excluded.content_words
            """,
            (cache_key, role_name, donor_id, query_text, generated_sql,
             json.dumps(data, default=str), now, now, embedding_json,
             entity_signature_json, content_words_json),
        )
        conn.commit()
    finally:
        conn.close()


def invalidate(query_text, role_name, donor_id):
    """
    Remove a single stale/invalid entry by recomputing its key from the
    original question text. Kept for exact-match invalidation call
    sites; if you already have the cache_key (e.g. from a semantic match
    result, whose key was NOT derived from the current question text),
    use invalidate_by_key() instead so the right row is removed.
    """
    invalidate_by_key(_cache_key(query_text, role_name, donor_id))


def invalidate_by_key(cache_key):
    """Safe to call even if the entry no longer exists."""
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM query_cache WHERE cache_key = ?", (cache_key,))
        conn.commit()
    finally:
        conn.close()


def clear_cache(role_name=None):
    """
    Wipe the cache. If role_name is given, only that role's entries are
    cleared (e.g. an admin clearing just the donor-facing cache after a
    schema change), otherwise everything is cleared.
    """
    conn = _get_connection()
    try:
        if role_name is None:
            conn.execute("DELETE FROM query_cache")
        else:
            conn.execute("DELETE FROM query_cache WHERE role_name = ?", (role_name,))
        conn.commit()
    finally:
        conn.close()

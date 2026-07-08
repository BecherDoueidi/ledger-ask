"""
Query analytics: a structured, queryable log of every request that hits
POST /api/generate-sql, regardless of which path it took (cache hit,
catalog hit, successful LLM generation, blocked, or failed).

This is intentionally separate from staging_queue.py:
- staging_queue is a REVIEW WORKLIST (only fresh LLM-generated questions,
  used to drive the admin Approve/Promote UI). Cache/catalog hits are
  deliberately excluded from it, because they've already been reviewed.
- query_analytics is a COMPLETE OBSERVABILITY LOG (every single request,
  including cache/catalog hits, with latency and outcome), used to
  answer questions like "what's our cache hit rate?", "how often does
  the LLM fail against CorporateDonors specifically?", "what's p95
  latency for donor vs admin?". It is never used to drive UI actions,
  only to diagnose and measure the system.

Keeping them separate means neither has to compromise its schema or
purpose to serve the other's use case.
"""

import sqlite3
import os
import json
from datetime import datetime, timezone
from contextlib import contextmanager

ANALYTICS_DB_PATH = os.path.join(os.path.dirname(__file__), "query_analytics.db")


def _get_connection():
    conn = sqlite3.connect(ANALYTICS_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS query_analytics (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            question           TEXT NOT NULL,
            role_name          TEXT,
            donor_id           INTEGER,
            path               TEXT NOT NULL,   -- 'catalog' | 'cache' | 'llm' | 'blocked' | 'error'
            cache_hit          INTEGER NOT NULL DEFAULT 0,
            llm_used           INTEGER NOT NULL DEFAULT 0,
            generated_sql      TEXT,
            success            INTEGER NOT NULL,
            failure_reason     TEXT,
            retries_used       INTEGER NOT NULL DEFAULT 0,
            rows_returned      INTEGER,
            schema_harvest_ms  INTEGER,
            llm_ms             INTEGER,
            db_exec_ms         INTEGER,
            total_ms           INTEGER NOT NULL,
            created_at         TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qa_created_at ON query_analytics(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qa_path ON query_analytics(path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_qa_success ON query_analytics(success)")
    return conn


class QueryTimer:
    """
    Tiny helper to accumulate wall-clock time for each phase of a
    request (schema harvesting, LLM call, DB execution) without
    littering app.py with manual time.time() bookkeeping at every call
    site. Usage:

        timer = QueryTimer()
        with timer.phase("schema_harvest"):
            ...
        with timer.phase("llm"):
            ...

    Phases can be entered multiple times (e.g. "llm" across retries);
    time accumulates rather than being overwritten.
    """

    def __init__(self):
        self._totals = {}
        self._start = datetime.now(timezone.utc)

    @contextmanager
    def phase(self, name):
        t0 = datetime.now(timezone.utc)
        try:
            yield
        finally:
            elapsed_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
            self._totals[name] = self._totals.get(name, 0) + elapsed_ms

    def ms(self, name):
        return self._totals.get(name)

    def total_ms(self):
        return int((datetime.now(timezone.utc) - self._start).total_seconds() * 1000)


def log_query(
    question,
    role_name,
    donor_id,
    path,
    success,
    timer,
    generated_sql=None,
    failure_reason=None,
    retries_used=0,
    rows_returned=None,
):
    """
    Write one row to the analytics log. Call this exactly once per
    request, at whichever exit point the request actually took.

    path: 'catalog' | 'cache' | 'llm' | 'blocked' | 'error'
    timer: a QueryTimer instance tracked across the request's lifetime.
    """
    cache_hit = 1 if path == "cache" else 0
    llm_used = 1 if path == "llm" else 0

    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO query_analytics (
                question, role_name, donor_id, path, cache_hit, llm_used,
                generated_sql, success, failure_reason, retries_used,
                rows_returned, schema_harvest_ms, llm_ms, db_exec_ms,
                total_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question, role_name, donor_id, path, cache_hit, llm_used,
                generated_sql, int(bool(success)), failure_reason, retries_used,
                rows_returned, timer.ms("schema_harvest"), timer.ms("llm"), timer.ms("db_exec"),
                timer.total_ms(), datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_summary(limit_days=None):
    """
    Aggregate stats for a quick health check: overall + broken down by
    path, plus the most common failure reasons. Powers GET /api/analytics/summary.
    """
    conn = _get_connection()
    try:
        where = ""
        params = ()
        if limit_days:
            where = "WHERE created_at >= datetime('now', ?)"
            params = (f"-{int(limit_days)} days",)

        totals = conn.execute(
            f"""
            SELECT
                COUNT(*)                                   AS total_requests,
                SUM(cache_hit)                              AS cache_hits,
                SUM(llm_used)                                AS llm_calls,
                SUM(success)                                 AS successes,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                AVG(total_ms)                                 AS avg_total_ms,
                AVG(CASE WHEN llm_used = 1 THEN llm_ms END)   AS avg_llm_ms,
                AVG(retries_used)                             AS avg_retries
            FROM query_analytics {where}
            """,
            params,
        ).fetchone()

        by_path = conn.execute(
            f"""
            SELECT path, COUNT(*) AS count,
                   SUM(success) AS successes,
                   AVG(total_ms) AS avg_ms
            FROM query_analytics {where}
            GROUP BY path
            """,
            params,
        ).fetchall()

        top_failures = conn.execute(
            f"""
            SELECT failure_reason, COUNT(*) AS count
            FROM query_analytics
            {where + ' AND' if where else 'WHERE'} success = 0 AND failure_reason IS NOT NULL
            GROUP BY failure_reason
            ORDER BY count DESC
            LIMIT 10
            """,
            params,
        ).fetchall()

        return {
            "total_requests": totals[0] or 0,
            "cache_hits": totals[1] or 0,
            "cache_hit_rate": round((totals[1] or 0) / totals[0], 3) if totals[0] else 0,
            "llm_calls": totals[2] or 0,
            "successes": totals[3] or 0,
            "failures": totals[4] or 0,
            "success_rate": round((totals[3] or 0) / totals[0], 3) if totals[0] else 0,
            "avg_total_ms": round(totals[5], 1) if totals[5] is not None else None,
            "avg_llm_ms": round(totals[6], 1) if totals[6] is not None else None,
            "avg_retries": round(totals[7], 2) if totals[7] is not None else 0,
            "by_path": [
                {"path": r[0], "count": r[1], "successes": r[2], "avg_ms": round(r[3], 1) if r[3] else None}
                for r in by_path
            ],
            "top_failure_reasons": [
                {"reason": r[0], "count": r[1]} for r in top_failures
            ],
        }
    finally:
        conn.close()


def get_recent(limit=50, path=None, success=None):
    """Raw recent rows, optionally filtered -- powers GET /api/analytics/recent."""
    conn = _get_connection()
    try:
        clauses, params = [], []
        if path:
            clauses.append("path = ?")
            params.append(path)
        if success is not None:
            clauses.append("success = ?")
            params.append(int(bool(success)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        rows = conn.execute(
            f"""
            SELECT id, question, role_name, donor_id, path, cache_hit, llm_used,
                   generated_sql, success, failure_reason, retries_used, rows_returned,
                   schema_harvest_ms, llm_ms, db_exec_ms, total_ms, created_at
            FROM query_analytics {where}
            ORDER BY id DESC LIMIT ?
            """,
            params,
        ).fetchall()

        cols = [
            "id", "question", "role_name", "donor_id", "path", "cache_hit", "llm_used",
            "generated_sql", "success", "failure_reason", "retries_used", "rows_returned",
            "schema_harvest_ms", "llm_ms", "db_exec_ms", "total_ms", "created_at",
        ]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()

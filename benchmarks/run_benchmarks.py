"""
Standalone benchmark script for the request-resolution pipeline. Not
part of the pytest suite -- it deliberately runs many iterations and
prints a report, which is a different job than pass/fail testing. Run
it directly:

    python benchmarks/run_benchmarks.py

Uses the same hermetic setup as the test suite (a temp SQLite database
standing in for MySQL, a faked LLM) so the numbers are reproducible and
never depend on a live Ollama daemon or MySQL server being reachable --
and specifically so the "fresh LLM generation" number measures THIS
CODEBASE's own pipeline overhead (schema harvest, prompt building,
security checks, DB execution), not actual model inference latency.
Real inference time is external, non-deterministic, and hardware/model
-dependent -- a number this project doesn't control and shouldn't claim
credit or blame for.
"""

import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text as sql_text

import auth
import catalog_manager
import conversation_state
import embeddings
import query_analytics
import query_cache
import query_service
import schema_harvester
import staging_queue
from routes import admin_api

DEFAULT_ITERATIONS = 50


def _setup_business_engine():
    db_fd, db_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(db_fd)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(sql_text(
            "CREATE TABLE Donors (DonorId INTEGER PRIMARY KEY, FullName TEXT, Email TEXT, Status TEXT)"
        ))
        conn.execute(sql_text(
            "CREATE TABLE Donations (DonationId INTEGER PRIMARY KEY, DonorId INTEGER, DonationAmount REAL, DonationDate TEXT)"
        ))
        conn.execute(sql_text(
            "INSERT INTO Donors (DonorId, FullName, Email, Status) VALUES (1, 'Ahmed Ali', 'a@x.com', 'Active')"
        ))
        conn.execute(sql_text(
            "INSERT INTO Donations (DonationId, DonorId, DonationAmount, DonationDate) VALUES (1, 1, 100.0, '2026-01-01')"
        ))
    return engine, db_path


def _isolate_stores(tmp_dir):
    """Same idea as conftest.py's isolate_sqlite_stores fixture, just without pytest's monkeypatch."""
    query_cache.CACHE_DB_PATH = os.path.join(tmp_dir, "query_cache.db")
    conversation_state.CONVERSATION_DB_PATH = os.path.join(tmp_dir, "conversation_state.db")
    query_analytics.ANALYTICS_DB_PATH = os.path.join(tmp_dir, "query_analytics.db")
    staging_queue.STAGING_DB_PATH = os.path.join(tmp_dir, "staging_queue.db")
    auth.USERS_DB_PATH = os.path.join(tmp_dir, "users.db")
    catalog_manager.CATALOG_PATH = os.path.join(tmp_dir, "catalog.yaml")


def _time_n(fn, n):
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return samples


def _percentile(sorted_samples, pct):
    return sorted_samples[min(len(sorted_samples) - 1, int(round(pct * (len(sorted_samples) - 1))))]


def _summarize(name, samples, verbose):
    ordered = sorted(samples)
    summary = {
        "path": name, "n": len(samples), "mean_ms": statistics.mean(samples),
        "p50_ms": _percentile(ordered, 0.50), "p95_ms": _percentile(ordered, 0.95),
        "min_ms": min(samples), "max_ms": max(samples),
    }
    if verbose:
        print(
            f"{name:38s}  n={summary['n']:<4d}  mean={summary['mean_ms']:8.2f}ms  "
            f"p50={summary['p50_ms']:8.2f}ms  p95={summary['p95_ms']:8.2f}ms  "
            f"min={summary['min_ms']:8.2f}ms  max={summary['max_ms']:8.2f}ms"
        )
    return summary


def run(iterations=DEFAULT_ITERATIONS, verbose=True):
    """
    Runs all four benchmark scenarios and returns a list of result dicts
    (one per path). Importable so a test can call it with a small
    iteration count to prove the whole thing actually executes, without
    needing to duplicate the setup here.
    """
    tmp_dir = tempfile.mkdtemp()
    _isolate_stores(tmp_dir)
    embeddings.get_embedding = lambda text: None  # hermetic, matches the test suite's default

    business_engine, db_path = _setup_business_engine()
    query_service.engine = business_engine
    schema_harvester.engine = business_engine
    admin_api.engine = business_engine

    fixed_sql = "SELECT COUNT(*) FROM Donors"
    query_service.call_llm_api = lambda system_prompt, user_query, temperature=0.0: fixed_sql

    if verbose:
        # Plain ASCII, not "Ledger·Ask" -- the middle dot mojibakes on
        # the default Windows console codepage (cp1252) this script is
        # primarily developed/run against, e.g. "Ledger?Ask".
        print(f"\nLedgerAsk pipeline benchmark ({iterations} iterations per path)")
        print("-" * 100)

    results = []
    try:
        # Fresh LLM generation path: a unique question every iteration
        # forces a cache miss every time, so this measures the full
        # schema-harvest -> prompt-build -> (faked) LLM -> security ->
        # DB-execute -> cache-write pipeline, minus real inference time.
        samples = _time_n(
            lambda: query_service.handle_generate_sql(
                f"how many donors {time.perf_counter_ns()}", "admin", None, "bench-fresh"
            ),
            iterations,
        )
        results.append(_summarize("Fresh LLM generation (pipeline only)", samples, verbose))

        # Exact cache hit: prime once, then repeat the identical question.
        query_service.handle_generate_sql("how many donors total", "admin", None, "bench-cache")
        samples = _time_n(
            lambda: query_service.handle_generate_sql("how many donors total", "admin", None, "bench-cache"),
            iterations,
        )
        results.append(_summarize("Exact cache hit", samples, verbose))

        # Catalog hit: an admin-promoted, pre-vetted question.
        catalog_manager.promote("how many donors are there", fixed_sql)
        samples = _time_n(
            lambda: query_service.handle_generate_sql("how many donors are there", "admin", None, "bench-catalog"),
            iterations,
        )
        results.append(_summarize("Catalog hit", samples, verbose))

        # In-memory follow-up transform (Tier 1: zero AI/DB cost).
        query_service.call_llm_api = lambda system_prompt, user_query, temperature=0.0: "SELECT FullName FROM Donors"
        query_service.handle_generate_sql("show me all donors", "admin", None, "bench-followup")
        samples = _time_n(
            lambda: query_service.handle_generate_sql("sort them by name", "admin", None, "bench-followup"),
            iterations,
        )
        results.append(_summarize("In-memory follow-up transform", samples, verbose))

        if verbose:
            print("-" * 100)
            print("Note: 'Fresh LLM generation' fakes the model call to return instantly, so it")
            print("measures this codebase's own pipeline overhead -- real Ollama inference time")
            print("is external, non-deterministic, and hardware-dependent, so it's excluded here.")
    finally:
        business_engine.dispose()
        os.unlink(db_path)

    return results


if __name__ == "__main__":
    run()

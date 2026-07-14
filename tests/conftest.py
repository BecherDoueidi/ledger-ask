"""
Shared pytest fixtures.

Two big rules this file enforces for every test in the suite:

1. NEVER touch the real *.db files in the project root. Every SQLite-backed
   module (query_cache, conversation_state, query_analytics, staging_queue,
   auth, catalog_manager) reads its path from a module-level constant at
   call time, so redirecting that constant to a tmp_path file before each
   test is enough -- no test ever needs to know this happens.
2. NEVER depend on a live Ollama daemon or a live MySQL server. Integration
   tests get an in-memory/temp-file SQLite "business" database standing in
   for the real MySQL one, and call_llm_api is monkeypatched to a
   controllable fake. embeddings.get_embedding defaults to returning None
   (simulating "no embedding model available") so semantic-cache tests are
   explicit about when they're exercising that path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, text as sql_text

import query_cache
import conversation_state
import query_analytics
import staging_queue
import auth
import catalog_manager
import embeddings


@pytest.fixture(autouse=True)
def isolate_sqlite_stores(tmp_path, monkeypatch):
    """
    Autouse: every test in the suite gets its own fresh, empty SQLite
    file per store, regardless of whether the test asks for it. This is
    what makes it safe to run this suite against a real checkout without
    ever risking the real query_cache.db / users.db / etc.
    """
    monkeypatch.setattr(query_cache, "CACHE_DB_PATH", str(tmp_path / "query_cache.db"))
    monkeypatch.setattr(conversation_state, "CONVERSATION_DB_PATH", str(tmp_path / "conversation_state.db"))
    monkeypatch.setattr(query_analytics, "ANALYTICS_DB_PATH", str(tmp_path / "query_analytics.db"))
    monkeypatch.setattr(staging_queue, "STAGING_DB_PATH", str(tmp_path / "staging_queue.db"))
    monkeypatch.setattr(auth, "USERS_DB_PATH", str(tmp_path / "users.db"))
    monkeypatch.setattr(catalog_manager, "CATALOG_PATH", str(tmp_path / "catalog.yaml"))


@pytest.fixture(autouse=True)
def no_real_embeddings(monkeypatch):
    """
    Default every test to "embeddings unavailable" (returns None), matching
    the app's own documented graceful-degradation behavior -- this keeps
    the suite hermetic (no dependency on a local Ollama daemon actually
    having an embedding model pulled). Tests that specifically want to
    exercise semantic matching override this themselves with a fake that
    returns deterministic vectors.
    """
    monkeypatch.setattr(embeddings, "get_embedding", lambda text: None)


@pytest.fixture
def business_engine():
    """
    A temp-file SQLite engine standing in for the real MySQL business
    database, with just enough schema/seed data to exercise role-based
    table access and row-level filtering meaningfully. File-based (not
    ':memory:') because app.py opens a NEW connection per request via
    engine.connect(), and an in-memory SQLite database is only visible
    to the connection that created it -- a fresh in-memory DB per
    connection would appear empty on every request after the first.
    """
    import tempfile
    db_fd, db_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(db_fd)
    engine = create_engine(f"sqlite:///{db_path}")

    with engine.begin() as conn:
        conn.execute(sql_text("""
            CREATE TABLE Donors (
                DonorId INTEGER PRIMARY KEY, FullName TEXT, Email TEXT, Status TEXT
            )
        """))
        conn.execute(sql_text("""
            CREATE TABLE Donations (
                DonationId INTEGER PRIMARY KEY, DonorId INTEGER, DonationAmount REAL, DonationDate TEXT
            )
        """))
        conn.execute(sql_text("CREATE TABLE Sponsorships (SponsorshipId INTEGER PRIMARY KEY, DonorId INTEGER)"))
        conn.execute(sql_text("CREATE TABLE EventDonations (EventDonationId INTEGER PRIMARY KEY, DonorId INTEGER)"))
        # Admin-only table -- never in roles_config's donor allowed_tables,
        # used to assert a donor-scoped query can't reach it.
        conn.execute(sql_text("CREATE TABLE Beneficiaries (BeneficiaryId INTEGER PRIMARY KEY, FullName TEXT)"))

        conn.execute(sql_text(
            "INSERT INTO Donors (DonorId, FullName, Email, Status) VALUES "
            "(1, 'Ahmed Ali', 'ahmed@example.com', 'Active'), "
            "(2, 'Sara Khalid', 'sara@example.com', 'Active')"
        ))
        conn.execute(sql_text(
            "INSERT INTO Donations (DonationId, DonorId, DonationAmount, DonationDate) VALUES "
            "(1, 1, 100.0, '2026-01-01'), (2, 1, 200.0, '2026-02-01'), (3, 2, 50.0, '2026-01-15')"
        ))
        conn.execute(sql_text("INSERT INTO Beneficiaries (BeneficiaryId, FullName) VALUES (1, 'Some Beneficiary')"))

    yield engine
    engine.dispose()
    os.unlink(db_path)


@pytest.fixture
def app_module(isolate_sqlite_stores, monkeypatch, business_engine):
    """
    Imports app.py with every module-level `engine` binding pointed at
    the temp SQLite business database instead of the real MySQL one.
    app.py itself is a thin factory now (see routes/, query_service.py)
    -- the actual pipeline lives in query_service.py, and
    routes/admin_api.py has its own independent `from db_config import
    engine` binding too (used by donor-account creation's identity-table
    check) -- each of these took its own copy at import time via `from
    db_config import engine`, so each needs redirecting independently,
    same as schema_harvester's copy.

    Explicitly depends on isolate_sqlite_stores (rather than trusting
    autouse-fixture ordering) and re-seeds default users on every call:
    `import app` only actually runs app.py's top-level
    `auth.seed_default_users()` the FIRST time any test imports it in
    this process -- every later test gets a fresh, empty tmp users.db
    (a new isolate_sqlite_stores per test) but the module is already
    cached in sys.modules, so that seeding call never fires again.
    Without this, login-dependent tests would pass or fail depending on
    which order pytest happens to run them in.
    """
    import app as app_module
    import schema_harvester
    import query_service
    from routes import admin_api

    monkeypatch.setattr(query_service, "engine", business_engine)
    monkeypatch.setattr(schema_harvester, "engine", business_engine)
    monkeypatch.setattr(admin_api, "engine", business_engine)
    auth.seed_default_users()
    return app_module


@pytest.fixture
def fake_llm(app_module, monkeypatch):
    """
    Replaces call_llm_api with a controllable fake. Usage:

        fake_llm.returns("SELECT ...")             # every call returns this
        fake_llm.returns_sequence(["bad", "good"])  # first call bad, retry good
        fake_llm.calls                              # list of (system_prompt, user_query, temperature)
    """
    import query_service

    class FakeLLM:
        def __init__(self):
            self.calls = []
            self._responses = None
            self._fixed = "SELECT 1"

        def returns(self, sql):
            self._fixed = sql
            self._responses = None

        def returns_sequence(self, sqls):
            self._responses = list(sqls)

        def __call__(self, system_prompt, user_query, temperature=0.0):
            self.calls.append((system_prompt, user_query, temperature))
            if self._responses:
                return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
            return self._fixed

    fake = FakeLLM()
    monkeypatch.setattr(query_service, "call_llm_api", fake)
    return fake


@pytest.fixture
def client(app_module):
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})

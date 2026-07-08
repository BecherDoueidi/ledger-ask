"""
schema_harvester.py -- live schema introspection (extract_live_metadata),
dynamic row-scoped table discovery, and the schema-fingerprint hash used
to detect a database swap. discover_row_scoped_tables and
compute_schema_fingerprint are the two functions written specifically to
let the app adapt to a different/changed database without code changes
-- see roles_config.py's resolve_allowed_tables and app.py's
cache-fingerprint check for how they're used.
"""

import schema_harvester


class TestDiscoverRowScopedTables:
    def test_finds_every_table_with_the_column(self, business_engine, monkeypatch):
        monkeypatch.setattr(schema_harvester, "engine", business_engine)
        tables = schema_harvester.discover_row_scoped_tables("DonorId")
        assert set(tables) == {"Donors", "Donations", "Sponsorships", "EventDonations"}

    def test_excludes_tables_without_the_column(self, business_engine, monkeypatch):
        monkeypatch.setattr(schema_harvester, "engine", business_engine)
        tables = schema_harvester.discover_row_scoped_tables("DonorId")
        assert "Beneficiaries" not in tables

    def test_case_insensitive_column_match(self, business_engine, monkeypatch):
        # Regression: a database that happens to lowercase/uppercase
        # column names differently from the original schema shouldn't
        # silently lose row-level scoping.
        monkeypatch.setattr(schema_harvester, "engine", business_engine)
        tables = schema_harvester.discover_row_scoped_tables("donorid")
        assert "Donors" in tables

    def test_no_matching_tables_returns_empty_list(self, business_engine, monkeypatch):
        monkeypatch.setattr(schema_harvester, "engine", business_engine)
        assert schema_harvester.discover_row_scoped_tables("NoSuchColumn") == []


class TestComputeSchemaFingerprint:
    def test_same_schema_text_produces_same_fingerprint(self):
        text = "Table: Donors\n  - DonorId (INTEGER)\n"
        assert (
            schema_harvester.compute_schema_fingerprint(text)
            == schema_harvester.compute_schema_fingerprint(text)
        )

    def test_different_schema_text_produces_different_fingerprint(self):
        # This is the whole point: adding/removing/renaming a column
        # must change the fingerprint, or a stale cache entry could
        # survive a real schema change undetected.
        before = "Table: Donors\n  - DonorId (INTEGER)\n"
        after = "Table: Donors\n  - DonorId (INTEGER)\n  - Email (TEXT)\n"
        assert (
            schema_harvester.compute_schema_fingerprint(before)
            != schema_harvester.compute_schema_fingerprint(after)
        )

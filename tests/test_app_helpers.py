"""
Pure-logic guards and repair helpers, now living in sql_safety.py: the
security matrix, the non-SQL output guard, markdown-fence stripping,
and the self-healing loop's unknown-column hint builder. None of these
need a DB or LLM, which is why they're tested directly against
sql_safety rather than through the full request pipeline (and no longer
need the app_module fixture at all, now that they don't live on the
Flask app module).
"""

import sql_safety


def test_violates_security_matrix_blocks_destructive_keywords():
    for bad in ["DROP TABLE Donors", "ALTER TABLE x", "DELETE FROM Donors", "TRUNCATE Donors",
                "SELECT 1; DROP TABLE x", "SELECT * FROM x -- comment", "SELECT 1 UNION SELECT 2"]:
        assert sql_safety.violates_security_matrix(bad) is True


def test_violates_security_matrix_allows_normal_select():
    assert sql_safety.violates_security_matrix("SELECT SUM(DonationAmount) FROM Donations") is False


def test_looks_like_sql():
    assert sql_safety.looks_like_sql("SELECT * FROM Donors") is True
    assert sql_safety.looks_like_sql("  select * from donors") is True
    assert sql_safety.looks_like_sql("I'm not sure what you mean") is False
    assert sql_safety.looks_like_sql("Sure! Here you go: SELECT * FROM x") is False


class TestStripMarkdownFence:
    def test_strips_sql_fence(self):
        raw = "```sql\nSELECT 1\n```"
        assert sql_safety.strip_markdown_fence(raw) == "SELECT 1"

    def test_strips_bare_fence(self):
        raw = "```\nSELECT 1\n```"
        assert sql_safety.strip_markdown_fence(raw) == "SELECT 1"

    def test_leaves_unfenced_sql_untouched(self):
        assert sql_safety.strip_markdown_fence("SELECT 1") == "SELECT 1"

    def test_real_world_regression_case(self):
        # The exact model output that was rejected as "non-SQL" before
        # this fix -- a real fenced multi-line query with a trailing
        # semicolon inside the fence.
        raw = (
            "```sql\n"
            "SELECT d.FullName, SUM(don.DonationAmount) AS TotalDonations\n"
            "FROM donors d\n"
            "JOIN donations don ON d.DonorId = don.DonorId\n"
            "GROUP BY d.FullName\n"
            "ORDER BY d.FullName ASC;\n"
            "```"
        )
        cleaned = sql_safety.strip_markdown_fence(raw)
        assert sql_safety.looks_like_sql(cleaned) is True
        assert cleaned.startswith("SELECT d.FullName")


class TestBuildUnknownColumnHint:
    SCHEMA = "Table: donations\n  - DonationAmount (DECIMAL)\n  - DonorId (INTEGER)\nTable: donors\n  - FullName (VARCHAR)\n  - DonorId (INTEGER)\n"

    def test_identifies_column_on_wrong_table(self):
        sql = "SELECT d.FullName, SUM(d.DonationAmount) FROM donations d"
        error = "Unknown column 'd.FullName' in 'field list'"
        hint, bad_column = sql_safety.build_unknown_column_hint(error, sql, self.SCHEMA)
        assert bad_column == "FullName"
        assert "donors" in hint

    def test_pure_hallucination_not_found_anywhere(self):
        sql = "SELECT d.DonorName FROM donations d"
        error = "Unknown column 'd.DonorName' in 'field list'"
        hint, bad_column = sql_safety.build_unknown_column_hint(error, sql, self.SCHEMA)
        assert bad_column == "DonorName"
        assert "does not exist anywhere" in hint

    def test_non_unknown_column_error_returns_empty(self):
        hint, bad_column = sql_safety.build_unknown_column_hint("Syntax error near SELECT", "SELECT 1", self.SCHEMA)
        assert hint == ""
        assert bad_column is None

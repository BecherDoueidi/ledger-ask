"""
Pure-logic helper functions defined in app.py: the security matrix, the
non-SQL output guard, markdown-fence stripping, and the self-healing
loop's unknown-column hint builder. None of these need a DB or LLM, so
they're tested directly rather than through the full request pipeline.
"""


def test_violates_security_matrix_blocks_destructive_keywords(app_module):
    for bad in ["DROP TABLE Donors", "ALTER TABLE x", "DELETE FROM Donors", "TRUNCATE Donors",
                "SELECT 1; DROP TABLE x", "SELECT * FROM x -- comment", "SELECT 1 UNION SELECT 2"]:
        assert app_module.violates_security_matrix(bad) is True


def test_violates_security_matrix_allows_normal_select(app_module):
    assert app_module.violates_security_matrix("SELECT SUM(DonationAmount) FROM Donations") is False


def test_looks_like_sql(app_module):
    assert app_module.looks_like_sql("SELECT * FROM Donors") is True
    assert app_module.looks_like_sql("  select * from donors") is True
    assert app_module.looks_like_sql("I'm not sure what you mean") is False
    assert app_module.looks_like_sql("Sure! Here you go: SELECT * FROM x") is False


class TestStripMarkdownFence:
    def test_strips_sql_fence(self, app_module):
        raw = "```sql\nSELECT 1\n```"
        assert app_module.strip_markdown_fence(raw) == "SELECT 1"

    def test_strips_bare_fence(self, app_module):
        raw = "```\nSELECT 1\n```"
        assert app_module.strip_markdown_fence(raw) == "SELECT 1"

    def test_leaves_unfenced_sql_untouched(self, app_module):
        assert app_module.strip_markdown_fence("SELECT 1") == "SELECT 1"

    def test_real_world_regression_case(self, app_module):
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
        cleaned = app_module.strip_markdown_fence(raw)
        assert app_module.looks_like_sql(cleaned) is True
        assert cleaned.startswith("SELECT d.FullName")


class TestBuildUnknownColumnHint:
    SCHEMA = "Table: donations\n  - DonationAmount (DECIMAL)\n  - DonorId (INTEGER)\nTable: donors\n  - FullName (VARCHAR)\n  - DonorId (INTEGER)\n"

    def test_identifies_column_on_wrong_table(self, app_module):
        sql = "SELECT d.FullName, SUM(d.DonationAmount) FROM donations d"
        error = "Unknown column 'd.FullName' in 'field list'"
        hint, bad_column = app_module.build_unknown_column_hint(error, sql, self.SCHEMA)
        assert bad_column == "FullName"
        assert "donors" in hint

    def test_pure_hallucination_not_found_anywhere(self, app_module):
        sql = "SELECT d.DonorName FROM donations d"
        error = "Unknown column 'd.DonorName' in 'field list'"
        hint, bad_column = app_module.build_unknown_column_hint(error, sql, self.SCHEMA)
        assert bad_column == "DonorName"
        assert "does not exist anywhere" in hint

    def test_non_unknown_column_error_returns_empty(self, app_module):
        hint, bad_column = app_module.build_unknown_column_hint("Syntax error near SELECT", "SELECT 1", self.SCHEMA)
        assert hint == ""
        assert bad_column is None

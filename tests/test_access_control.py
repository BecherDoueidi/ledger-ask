"""
access_control.py -- the actual security boundary (not the schema
hiding in the LLM prompt). These tests specifically probe the edge
cases the module's own comments call out: the alias-vs-keyword
ambiguity in the row-level-filter regex, and case-insensitivity.
"""

import access_control as ac


class TestExtractReferencedTables:
    def test_from_clause(self):
        assert ac.extract_referenced_tables("SELECT * FROM Donors") == {"Donors"}

    def test_join_clause(self):
        tables = ac.extract_referenced_tables("SELECT * FROM Donors d JOIN Donations o ON d.DonorId = o.DonorId")
        assert tables == {"Donors", "Donations"}

    def test_case_insensitive_keywords(self):
        assert ac.extract_referenced_tables("select * from Donors") == {"Donors"}

    def test_backtick_quoted_table_name(self):
        assert ac.extract_referenced_tables("SELECT * FROM `Donors`") == {"Donors"}


class TestCheckTableAccess:
    def test_unrestricted_role_allows_anything(self):
        ok, disallowed = ac.check_table_access("SELECT * FROM AnyTable", allowed_tables=None)
        assert ok is True
        assert disallowed == set()

    def test_restricted_role_allows_listed_tables(self):
        ok, disallowed = ac.check_table_access("SELECT * FROM Donors", allowed_tables=["Donors", "Donations"])
        assert ok is True

    def test_restricted_role_blocks_unlisted_table(self):
        ok, disallowed = ac.check_table_access("SELECT * FROM Beneficiaries", allowed_tables=["Donors"])
        assert ok is False
        assert disallowed == {"Beneficiaries"}

    def test_join_to_disallowed_table_is_blocked(self):
        sql = "SELECT * FROM Donors d JOIN Beneficiaries b ON d.DonorId = b.DonorId"
        ok, disallowed = ac.check_table_access(sql, allowed_tables=["Donors"])
        assert ok is False
        assert disallowed == {"Beneficiaries"}

    def test_case_insensitive_table_name_matching(self):
        ok, _ = ac.check_table_access("SELECT * FROM donors", allowed_tables=["Donors"])
        assert ok is True


class TestApplyRowLevelFilter:
    def test_wraps_table_with_row_filter(self):
        sql = ac.apply_row_level_filter("SELECT * FROM Donations", ["Donations"], "DonorId", 5)
        assert "(SELECT * FROM Donations WHERE DonorId = 5) AS Donations" in sql

    def test_preserves_existing_alias(self):
        sql = ac.apply_row_level_filter("SELECT * FROM Donations d", ["Donations"], "DonorId", 5)
        assert "(SELECT * FROM Donations WHERE DonorId = 5) AS d" in sql

    def test_does_not_consume_following_keyword_as_alias(self):
        # The regression this guards: a naive optional-alias regex would
        # swallow "WHERE" itself as if it were an alias, corrupting the
        # query. This is the negative-lookahead the module's docstring
        # calls "critical."
        sql = ac.apply_row_level_filter(
            "SELECT * FROM Donations WHERE DonationAmount > 100", ["Donations"], "DonorId", 5
        )
        assert "AS WHERE" not in sql
        assert "WHERE DonationAmount > 100" in sql

    def test_none_allowed_tables_is_a_noop(self):
        sql = "SELECT * FROM Donations"
        assert ac.apply_row_level_filter(sql, None, "DonorId", 5) == sql

    def test_none_row_filter_column_is_a_noop(self):
        sql = "SELECT * FROM Donations"
        assert ac.apply_row_level_filter(sql, ["Donations"], None, 5) == sql

    def test_composes_with_join(self):
        sql = ac.apply_row_level_filter(
            "SELECT * FROM Donors d JOIN Donations o ON d.DonorId = o.DonorId",
            ["Donors", "Donations"], "DonorId", 5,
        )
        assert "(SELECT * FROM Donors WHERE DonorId = 5) AS d" in sql
        assert "(SELECT * FROM Donations WHERE DonorId = 5) AS o" in sql

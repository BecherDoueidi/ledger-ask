"""
prompt_builder.py -- system prompt construction, including per-dialect
few-shot examples. Pure string-building, no DB/LLM required.
"""

import prompt_builder


class TestDialectCoverage:
    def test_known_dialects_get_specific_examples(self):
        for dialect in ["sqlite", "mysql", "postgresql", "mssql"]:
            prompt = prompt_builder.build_system_prompt(dialect, "Table: X\n", None, None)
            assert f"CORRECT SYNTAX EXAMPLES FOR {dialect.upper()}" in prompt

    def test_unknown_dialect_still_produces_a_valid_prompt(self):
        # Not every SQLAlchemy dialect has curated few-shot examples --
        # the model still gets the real dialect name and the shared
        # rules, it just skips the dialect-specific section gracefully.
        prompt = prompt_builder.build_system_prompt("oracle", "Table: X\n", None, None)
        assert "ORACLE" in prompt
        assert "CORRECT SYNTAX EXAMPLES" not in prompt

    def test_mssql_bans_limit_not_top(self):
        prompt = prompt_builder.build_system_prompt("mssql", "Table: X\n", None, None)
        assert "LIMIT does not exist" in prompt

    def test_mysql_still_requires_concat_and_year(self):
        # Regression: MySQL's few-shot rules must never accidentally
        # ban CONCAT()/YEAR() the way SQLite's do -- see the module's
        # per-dialect banned_syntax_note comments.
        prompt = prompt_builder.build_system_prompt("mysql", "Table: X\n", None, None)
        assert "CONCAT() for string concatenation" in prompt


class TestRoleContext:
    def test_unrestricted_role_has_no_restriction_notice(self):
        prompt = prompt_builder.build_system_prompt("mysql", "Table: X\n", None, None)
        assert "ROLE RESTRICTION NOTICE" not in prompt

    def test_restricted_role_gets_the_configured_filter_column(self):
        # Regression: this notice used to hardcode "DonorId" regardless
        # of the role's actual row_filter_column.
        prompt = prompt_builder.build_system_prompt(
            "mysql", "Table: X\n", ["Donations"], 5, row_filter_column="AccountId"
        )
        assert "WHERE AccountId = 5" in prompt
        assert "DonorId" not in prompt


class TestFollowupContext:
    def test_followup_context_included_when_present(self):
        prompt = prompt_builder.build_system_prompt(
            "mysql", "Table: X\n", None, None,
            followup_context={"last_question": "show donors", "last_sql": "SELECT * FROM Donors", "transform_log": []},
        )
        assert "FOLLOW-UP" in prompt
        assert "show donors" in prompt

    def test_no_followup_context_by_default(self):
        prompt = prompt_builder.build_system_prompt("mysql", "Table: X\n", None, None)
        assert "FOLLOW-UP" not in prompt

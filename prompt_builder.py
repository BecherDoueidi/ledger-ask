"""
Builds the text-to-SQL system prompt sent to the LLM: dialect rules,
few-shot syntax anchors, the role-scoping addendum, and the live schema.
Isolated from query_service.py because prompt construction is a distinct
concern from request orchestration -- it has no side effects and is
cheap to unit test on its own (see tests/test_prompt_builder.py).
"""

# Per-dialect banned-syntax note + few-shot examples. Keyed by the
# lowercased dialect name SQLAlchemy reports (engine.dialect.name).
# Dialects not listed here still work -- the model is still told the
# real dialect name and given the shared rules below -- they just don't
# get dialect-specific few-shot examples yet. See DIALECT_EXAMPLES's
# module-level docstring note in README.md's "Database portability"
# section for how to add one.
DIALECT_EXAMPLES = {
    "sqlite": {
        "banned_syntax_note": "Banned syntax includes TOP clauses, CONCAT functions, and YEAR() calls.",
        "few_shot": """
### CORRECT SYNTAX EXAMPLES FOR SQLITE:
User Query: Combine first and last names of the top 2 oldest employees and get their hire year.
Correct Response: SELECT FirstName || ' ' || LastName AS FullName, strftime('%Y', HireDate) AS HireYear FROM Employee ORDER BY BirthDate ASC LIMIT 2

User Query: Concatenate city and country for customers and limit to 5.
Correct Response: SELECT BillingCity || ', ' || BillingCountry FROM Invoice LIMIT 5
""",
    },
    "mysql": {
        # Banning TOP is still correct for MySQL (it doesn't exist here),
        # but CONCAT() and YEAR() are the CORRECT MySQL syntax -- they
        # must never be banned for this dialect, only for SQLite where
        # || and strftime() are used instead.
        "banned_syntax_note": "Banned syntax includes TOP clauses. Use CONCAT() for string concatenation and YEAR() for extracting a year -- these are correct and required for this dialect.",
        "few_shot": """
### CORRECT SYNTAX EXAMPLES FOR MYSQL:
User Query: Combine first and last name of the top 2 oldest employees and get their hire year.
Correct Response: SELECT CONCAT(FirstName, ' ', LastName) AS FullName, YEAR(HireDate) AS HireYear FROM Employee ORDER BY BirthDate ASC LIMIT 2

User Query: Show total contribution per corporate donor, top 5.
Correct Response: SELECT t1.CompanyName, SUM(t2.DonationAmount) AS TotalContribution FROM CorporateDonors t1 INNER JOIN Donations t2 ON t1.CorporateId = t2.CorporateId GROUP BY t1.CompanyName ORDER BY TotalContribution DESC LIMIT 5
# Note: a monetary-sounding column that exists on the donor/entity table itself (e.g. AnnualContribution)
# is a separate stored figure, NOT the same as summing transactional rows in a related table
# (e.g. Donations.DonationAmount). Never substitute one for the other -- check which table
# a column actually belongs to in the schema below before referencing it, especially across a JOIN.
""",
    },
    "postgresql": {
        # Postgres accepts both single and double quotes for strings vs
        # identifiers the opposite way MySQL is lenient about, and has
        # its own concatenation/date-part syntax -- calling this out
        # explicitly avoids the model defaulting to MySQL habits (e.g.
        # YEAR()) that don't exist here.
        "banned_syntax_note": "Banned syntax includes TOP clauses, YEAR(), and backtick-quoted identifiers (use double quotes only if an identifier must be quoted). Use || for string concatenation and EXTRACT(YEAR FROM col) for extracting a year.",
        "few_shot": """
### CORRECT SYNTAX EXAMPLES FOR POSTGRESQL:
User Query: Combine first and last names of the top 2 oldest employees and get their hire year.
Correct Response: SELECT FirstName || ' ' || LastName AS FullName, EXTRACT(YEAR FROM HireDate) AS HireYear FROM Employee ORDER BY BirthDate ASC LIMIT 2

User Query: Show total contribution per corporate donor, top 5.
Correct Response: SELECT t1.CompanyName, SUM(t2.DonationAmount) AS TotalContribution FROM CorporateDonors t1 INNER JOIN Donations t2 ON t1.CorporateId = t2.CorporateId GROUP BY t1.CompanyName ORDER BY TotalContribution DESC LIMIT 5
""",
    },
    "mssql": {
        # The inverse of every other dialect here: SQL Server is the one
        # place TOP is correct and LIMIT does not exist, which is worth
        # spelling out explicitly since the shared rules below otherwise
        # imply LIMIT is always how to cap row counts.
        "banned_syntax_note": "LIMIT does not exist in this dialect -- use TOP instead (e.g. SELECT TOP 5 ...). Use + for string concatenation and YEAR() for extracting a year.",
        "few_shot": """
### CORRECT SYNTAX EXAMPLES FOR MSSQL (SQL Server):
User Query: Combine first and last name of the top 2 oldest employees and get their hire year.
Correct Response: SELECT TOP 2 FirstName + ' ' + LastName AS FullName, YEAR(HireDate) AS HireYear FROM Employee ORDER BY BirthDate ASC

User Query: Show total contribution per corporate donor, top 5.
Correct Response: SELECT TOP 5 t1.CompanyName, SUM(t2.DonationAmount) AS TotalContribution FROM CorporateDonors t1 INNER JOIN Donations t2 ON t1.CorporateId = t2.CorporateId GROUP BY t1.CompanyName ORDER BY TotalContribution DESC
""",
    },
}


def build_system_prompt(db_dialect, live_schema, allowed_tables, donor_id, row_filter_column=None, followup_context=None):
    """
    Shared by BOTH a fresh question and an LLM-assisted follow-up
    modification (see the conversation-follow-up branch in
    query_service.py) so the dialect/security/schema rules never drift
    between the two call sites -- only followup_context differs.

    followup_context: {"last_question", "last_sql", "transform_log"} when
    this is tier-2 of a follow-up (the deterministic transform tier in
    followup_resolver.py couldn't satisfy it, e.g. it needs a new JOIN).
    None for a fresh, self-contained question -- today's original prompt,
    byte-for-byte.
    """
    dialect_info = DIALECT_EXAMPLES.get(db_dialect.lower(), {})
    banned_syntax_note = dialect_info.get("banned_syntax_note", "")
    few_shot_examples = dialect_info.get("few_shot", "")

    # Strict System Boundary Construction -- a role-specific addendum so
    # the LLM knows its SQL will be wrapped in a subquery filter. Without
    # this it writes SUM(d.col) with a table alias that no longer exists
    # after the rewrite, which produces NULL from aggregate functions
    # like SUM/COUNT/AVG.
    role_context = ""
    if allowed_tables is not None:
        role_context = f"""
ROLE RESTRICTION NOTICE: Your SQL will be executed inside a pre-filtered subquery.
Each table reference is automatically rewritten to:
  FROM (SELECT * FROM <table> WHERE {row_filter_column} = {donor_id}) AS <table>
Because of this you MUST follow these rules:
- NEVER use a table alias in aggregate functions. Write SUM(DonationAmount), NOT SUM(d.DonationAmount).
- NEVER prefix column names with a table name. Write DonationAmount, NOT Donations.DonationAmount.
- The filter is already applied -- do NOT add your own WHERE {row_filter_column} = ... clause.
"""

    conversation_context = ""
    if followup_context is not None:
        transform_note = ""
        if followup_context["transform_log"]:
            transform_note = (
                "Since that query ran, the following display-only transforms were applied on top "
                "of its result (these are NOT part of the SQL -- fold their intent into your new "
                "query if the follow-up implies keeping them, e.g. a still-relevant sort or filter): "
                + "; ".join(followup_context["transform_log"]) + ".\n"
            )
        conversation_context = f"""
CONVERSATION CONTEXT: The next user message is a FOLLOW-UP in an ongoing
conversation, not a standalone question.
The previous question was: "{followup_context['last_question']}"
It was answered with this SQL: {followup_context['last_sql']}
{transform_note}Write ONE complete, standalone SQL query that satisfies the follow-up in light of
that context -- extend, modify, or replace the previous query as needed. Do not describe a
diff; return the full query.
"""

    system_prompt = f"""You are an enterprise-grade Text-to-SQL compilation engine.
Your sole mandate is to convert natural language queries into valid, optimized SQL statements.

CRITICAL OPERATIONAL BOUNDARIES:
1. TARGET DIALECT: You are generating SQL for a '{db_dialect.upper()}' database. You MUST write strictly valid {db_dialect.upper()} syntax.
2. Follow the exact formatting patterns demonstrated in the examples below. {banned_syntax_note}
3. NEVER append a semicolon (;) to the end of your generated SQL string.
4. Before referencing any column, verify in the schema below which table it actually belongs to -- do not assume a column exists on a table just because it's semantically related (e.g. a summary/aggregate column stored on one table is not interchangeable with a transactional column on a related table).
5. MINIMALITY: Select ONLY the columns strictly required to answer the question. Do not add extra columns (names, emails, IDs, etc.) or extra JOINs "for context" unless the user explicitly asked for them. A question asking for a single total, count, or average should produce a single-column (or single-value) result -- e.g. "How much have I donated in total?" is exactly `SELECT SUM(DonationAmount) FROM Donations`, nothing more. Every extra column or JOIN you add is a chance to reference a column that doesn't exist.
{role_context}
{few_shot_examples}
{conversation_context}
Target Database Schema Context:
{live_schema}
"""
    return system_prompt

"""
Pure-logic guards and repair helpers applied to LLM input/output: the
input security fence, the non-SQL output guard, markdown-fence
stripping, and the self-healing retry loop's unknown-column hint
builder. None of these need a DB or LLM connection, which is why they
live here rather than in query_service.py -- they're cheap to unit test
in isolation (see tests/test_sql_safety.py).
"""

import re

_UNKNOWN_COLUMN_PATTERN = re.compile(r"(?i)unknown column '(?:([A-Za-z_]\w*)\.)?(\w+)'")
_ALIAS_TABLE_PATTERN = re.compile(r'(?i)\b(?:FROM|JOIN)\s+`?(\w+)`?(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?')
_SQL_LEADING_VERB_PATTERN = re.compile(r"(?i)^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b")
_MARKDOWN_FENCE_PATTERN = re.compile(r"^```(?:sql)?\s*\n?|\n?```$", re.IGNORECASE)


def violates_security_matrix(query):
    """
    Your defense layer. Inspects raw natural language or generated queries
    for injection threats before parsing.
    """
    malicious_patterns = [
        r"(?i)\bDROP\b",
        r"(?i)\bALTER\b",
        r"(?i)\bDELETE\b",
        r"(?i)\bTRUNCATE\b",
        r"(?i)--",
        r"(?i);",
        r"UNION\s+SELECT"
    ]
    for pattern in malicious_patterns:
        if re.search(pattern, query):
            return True
    return False


def strip_markdown_fence(text_output):
    """
    Strips a leading/trailing ```sql ... ``` (or bare ``` ... ```) code
    fence if the model wrapped its answer in one, e.g.:

        ```sql
        SELECT ...
        ```

    instead of the requested bare SQL string. Observed in practice on
    the follow-up-modify path (see prompt_builder.build_system_prompt's
    conversation_context) even though the base system prompt already
    says "do not include markdown formatting" -- a model can still
    decide to fence its output, and without this, looks_like_sql() then
    rejects a perfectly correct query as "non-SQL conversational output"
    just because of the ``` wrapper around it.
    """
    return _MARKDOWN_FENCE_PATTERN.sub("", text_output.strip()).strip()


def looks_like_sql(text_output):
    """
    True only if the LLM's output actually starts with a real SQL
    statement keyword. This exists because the model will sometimes
    respond conversationally instead of generating SQL (e.g. the user
    typed "hi" or an ambiguous non-question), and that conversational
    text is NOT a "broken SQL query" -- it was never SQL at all. Feeding
    it into the self-healing retry loop wastes a retry telling the model
    to "fix" text that was never meant to be SQL, and the model will
    often then hallucinate a plausible-looking but meaningless query
    (e.g. "SELECT * FROM some_table") just to comply -- which then
    executes successfully and gets shown to the user as a real,
    "freshly generated" answer to a question that was never actually
    asked. This check has to catch that BEFORE execution is attempted.
    """
    return bool(_SQL_LEADING_VERB_PATTERN.match(text_output.strip()))


def build_unknown_column_hint(error_msg, failed_sql, live_schema):
    """
    If the DB error is an "unknown column" error, figure out (a) which
    table the failing alias actually pointed at, and (b) which table(s)
    in the live schema really do have a column by that name -- then
    return an explicit, unambiguous correction string for the retry
    prompt. Returns ("", None) if the error isn't an unknown-column error or we
    can't confidently resolve it (in which case the retry falls back to
    the generic error message alone).

    This exists because handing the model the raw error text and schema
    and saying "fix it" isn't always enough: a plausible-sounding wrong
    column (e.g. a summary column on a related table) can get
    regenerated identically across retries even with temperature > 0.
    Spelling out "X does not exist on table Y, it exists on table Z" is
    a much harder signal for the model to ignore.
    """
    match = _UNKNOWN_COLUMN_PATTERN.search(error_msg)
    if not match:
        return "", None
    bad_alias, bad_column = match.group(1), match.group(2)

    # Map every alias/table name used in the failed query to its real table.
    alias_to_table = {}
    for tbl_match in _ALIAS_TABLE_PATTERN.finditer(failed_sql):
        table_name = tbl_match.group(1)
        alias = tbl_match.group(2) or table_name
        alias_to_table[alias.lower()] = table_name

    wrong_table = alias_to_table.get((bad_alias or "").lower())

    # Scan the live schema text for every table that actually has a
    # column with this exact name.
    correct_tables = []
    current_table = None
    for line in live_schema.splitlines():
        header = re.match(r"Table:\s*(\w+)", line)
        if header:
            current_table = header.group(1)
            continue
        col_match = re.match(r"\s*-\s*(\w+)\s*\(", line)
        if col_match and current_table and col_match.group(1).lower() == bad_column.lower():
            correct_tables.append(current_table)

    if not correct_tables:
        # We know the column doesn't exist where the model put it, but
        # it doesn't exist anywhere in the visible schema either -- it's
        # a pure hallucination, not a misattribution. Say so plainly.
        #
        # NOTE: an earlier version of this tried to auto-suggest the
        # "closest" real column name via string similarity (difflib).
        # Tested against this exact case: for hallucinated 'DonorName',
        # it confidently suggested 'DonorId' (0.625 similarity) over the
        # actually-correct 'FullName' (0.47) -- because 'DonorId' shares
        # more raw characters, despite being semantically wrong. A false
        # suggestion stated confidently is worse than no suggestion, so
        # this was removed rather than shipped. The forbidden_columns
        # hard-constraint mechanism in query_service.py (which doesn't
        # require guessing intent) is what actually prevents this from
        # looping.
        return (f"\nSPECIFIC CORRECTION: The column '{bad_column}' does not exist anywhere in the "
                f"schema below. Do not reuse this column name under any alias. Re-read the schema "
                f"and find the real column that holds this data.\n"), bad_column

    where_text = " or ".join(correct_tables)
    table_clause = f"table '{wrong_table}'" if wrong_table else "the table you used"
    return (f"\nSPECIFIC CORRECTION: The column '{bad_column}' does NOT exist on {table_clause}. "
            f"It exists on: {where_text}. If you need both this column and data from "
            f"{table_clause}, you must reference '{bad_column}' from {where_text} specifically "
            f"(not from the alias that failed) -- do not simply reuse the same column name on "
            f"the wrong table again.\n"), bad_column

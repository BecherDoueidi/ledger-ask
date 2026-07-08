"""
Real enforcement of role restrictions, applied to SQL AFTER the LLM
generates it. This is the layer that actually matters -- the schema
shown to the LLM is just a hint; this is what stops a clever or
hallucinated query from reaching a table/row it shouldn't.
"""

import re


def extract_referenced_tables(sql):
    """Returns the set of table names referenced via FROM/JOIN in this SQL."""
    pattern = re.compile(r'(?i)\b(?:FROM|JOIN)\s+`?(\w+)`?')
    return {match.group(1) for match in pattern.finditer(sql)}


def check_table_access(sql, allowed_tables):
    """
    allowed_tables: None means unrestricted. Otherwise a list of table
    names this role may touch.
    Returns (is_allowed: bool, disallowed_tables: set).
    """
    if allowed_tables is None:
        return True, set()

    allowed_set = {t.lower() for t in allowed_tables}
    referenced = extract_referenced_tables(sql)
    disallowed = {t for t in referenced if t.lower() not in allowed_set}
    return (len(disallowed) == 0), disallowed


_SQL_KEYWORDS_AFTER_TABLE = [
    "where", "join", "on", "group", "order", "limit", "having",
    "inner", "left", "right", "outer", "union", "as", "set", "values"
]


def apply_row_level_filter(sql, allowed_tables, row_filter_column, customer_id):
    """
    Wraps every reference to an allowed table in a pre-filtered subquery,
    e.g. "FROM Donations d" becomes
    "FROM (SELECT * FROM Donations WHERE DonorId = 5) AS d" -- preserving
    the original alias if one was given, or using the table name itself
    as the alias if it wasn't, so unqualified column references and any
    existing alias both keep working.

    This guarantees the row restriction applies even if the LLM never
    wrote a WHERE clause itself, and it composes correctly with JOINs.

    customer_id is validated as an integer before this is called, so it
    is safe to interpolate directly.
    """
    if row_filter_column is None or allowed_tables is None:
        return sql

    keyword_alternation = "|".join(_SQL_KEYWORDS_AFTER_TABLE)

    rewritten = sql
    for table in allowed_tables:
        # The negative lookahead is critical: it stops the optional alias
        # group from ever matching (and thereby consuming/deleting) a
        # following SQL keyword like WHERE, GROUP, ORDER, etc.
        pattern = re.compile(
            r'(?i)\b(FROM|JOIN)\s+`?' + re.escape(table) + r'`?'
            r'(?:\s+(?:AS\s+)?(?!(?:' + keyword_alternation + r')\b)([A-Za-z_]\w*))?'
        )

        def replace(match, table=table):
            clause_keyword = match.group(1)
            alias = match.group(2) or table
            return (
                f"{clause_keyword} (SELECT * FROM {table} "
                f"WHERE {row_filter_column} = {customer_id}) AS {alias}"
            )

        rewritten = pattern.sub(replace, rewritten)
    return rewritten

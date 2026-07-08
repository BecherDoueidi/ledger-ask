"""
Turns a follow-up question ("sort them by name", "only show active
donors", "create a bar chart") into either:

  (a) a direct transform of the previous result set, computed here in
      pure Python with NO database or LLM call, or
  (b) a signal that app.py should fall back to an LLM-assisted SQL
      MODIFICATION (see build_followup_system_prompt), because the
      request needs data that isn't in the current result at all
      (a new JOIN, a new aggregate), or
  (c) a signal that this isn't a follow-up at all -- a fresh,
      self-contained question that should go through the normal
      catalog/cache/LLM pipeline unchanged.

Everything in this module is deterministic (regex/keyword matching),
by design: the cost ladder for a follow-up is "free Python transform"
first, "one LLM call, but through the existing hardened pipeline"
second, and classification itself must not be the thing that makes
every follow-up expensive. See conversation_state.py for where the
"previous result" this operates on comes from, and app.py for how the
three outcomes above are wired into the request handler.

Known, accepted limitation: this is a heuristic classifier, not a full
NLU system. It's tuned against the phrasing patterns this app's own
examples use (sort/filter/limit/dedupe/chart, pronouns, "now"). Phrasing
far outside that (e.g. deeply nested conditional follow-ups) will
correctly fall through to "not a recognized transform" and escalate to
the LLM tier rather than silently doing the wrong thing -- a missed fast
path costs one extra LLM call, which is the safe direction to fail in.
"""

import re

import semantic_match

# --- Tier-0 classification: is this even a follow-up? ---------------------

_REFERENTIAL_PATTERN = re.compile(
    r"(?i)\b(them|these|those|it|their|this table|that table|the table|"
    r"the previous result|the last result|the prior result|the results?|"
    r"now)\b"
)
_LEADING_TRANSFORM_PATTERN = re.compile(
    r"(?i)^\s*(sort|order|arrange|rank|filter|limit|only show|show only|"
    r"remove duplicate|remove duplicates|dedupe|deduplicate|distinct|unique|"
    r"group by|chart|plot|visuali[sz]e|graph|make (it|this) a|create a)\b"
)


def is_followup(question, has_active_conversation):
    """
    True only if a live conversation exists AND the question shows a
    referential or elliptical-imperative signal. A brand new,
    self-contained question ("How many volunteers are there?") does NOT
    match either pattern and correctly returns False even mid-conversation,
    so switching topics doesn't get trapped trying to "transform" data
    that has nothing to do with the new question.
    """
    if not has_active_conversation:
        return False
    return bool(_REFERENTIAL_PATTERN.search(question) or _LEADING_TRANSFORM_PATTERN.search(question))


# --- Tier-1 operation classification ---------------------------------------

_SORT_PATTERN = re.compile(r"(?i)\b(sort|order|arrange|rank)\b")
_DESC_PATTERN = re.compile(r"(?i)\b(desc|descending|highest|largest|biggest|most|reverse)\b")
# Superset of _DESC_PATTERN plus the ascending-direction words -- used to
# strip a direction word out of the field text before resolving it against
# a real column name. "Sort by contribution ascending" must resolve the
# field as "contribution", not "contribution ascending": the latter fails
# to match "AnnualContribution" by substring (no word boundary was ever
# guaranteed to fall in a helpful place), and previously only "worked" for
# descending by the accident of "totaldonations" being a PREFIX of
# "totaldonationsdescending" -- not a real fix, just a coincidence that
# broke as soon as the direction word came first or the column name
# didn't happen to prefix-match.
_DIRECTION_WORD_PATTERN = re.compile(
    r"(?i)\b(asc|ascending|desc|descending|highest|lowest|largest|smallest|biggest|least|most|reverse)\b"
)
_LIMIT_PATTERN = re.compile(r"(?i)\b(?:first|top|only|limit)\s+(\d+)\b|\b(\d+)\s+(?:results?|rows?)\b")
_DEDUPE_PATTERN = re.compile(r"(?i)\b(remove duplicates?|dedupe|deduplicate|distinct|unique)\b")
_CHART_PATTERN = re.compile(r"(?i)\b(chart|plot|visuali[sz]e|graph)\b")
_CHART_TYPE_PATTERN = re.compile(r"(?i)\b(bar|line|pie|scatter|doughnut)\b")
_COLUMN_SELECT_PATTERN = re.compile(r"(?i)^\s*(?:just show|only show|show only)\s+(.+?)\s*$")

# Same rationale and shape as chart_advisor.py's _NUMERIC_STRING_PATTERN:
# a cache-hit round-trips MySQL DECIMAL values through JSON as strings
# (Decimal isn't JSON-native), always with a decimal point since every
# DECIMAL column in this schema has a fixed scale. Requiring the point
# is what excludes ID/phone-number-shaped digit strings from being
# treated as sortable numbers.
_NUMERIC_STRING_PATTERN = re.compile(r"^-?\d+\.\d+$")


def _looks_numeric_string(value):
    return isinstance(value, str) and bool(_NUMERIC_STRING_PATTERN.match(value.strip()))

# A tiny, explicit vocabulary of "adjective describing a row" -> the value
# it maps to on a status-like column. Deliberately small and explicit
# rather than guessed: if the word isn't here, or there's no status-like
# column in the current result, filtering escalates to the LLM tier
# instead of taking a wrong guess at what "active" means on data that
# doesn't have a status concept.
_STATUS_ADJECTIVES = {
    "active": "Active", "inactive": "Inactive", "approved": "Approved",
    "pending": "Pending", "rejected": "Rejected", "blocked": "Blocked",
    "completed": "Completed", "cancelled": "Cancelled", "canceled": "Cancelled",
}


def _strip_plural(word):
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9]")


def _normalize_field_text(text):
    """
    Lowercases, strips everything but letters/digits, and plural-strips
    the result. This is what lets a natural-language multi-word
    reference like "total donations" match a compound PascalCase column
    name like "TotalDonations" -- without stripping the space, "total
    donations" (with a space) never matches "totaldonations" (without
    one) via substring comparison, which silently sent every multi-word
    field reference to the expensive LLM-modify tier instead of the free
    in-Python transform, even though the column existed right there in
    the previous result.
    """
    return _strip_plural(_NON_ALNUM_PATTERN.sub("", text.lower()))


def resolve_column(requested_text, available_columns):
    """
    Fuzzy-matches a natural-language field reference ("name", "emails",
    "total donations") against the ACTUAL columns of the previous result
    ("FullName", "Email", "TotalDonations"). Substring matching after
    normalization is enough for this app's column-naming style (compound
    PascalCase names); returns None (never a guess) if nothing matches
    confidently.
    """
    target = _normalize_field_text(requested_text)
    if not target:
        return None
    best = None
    for col in available_columns:
        col_norm = _normalize_field_text(col)
        if target == col_norm:
            return col  # exact match wins outright
        if target in col_norm or col_norm in target:
            best = best or col
    return best


def find_status_like_column(available_columns):
    for col in available_columns:
        if "status" in col.lower():
            return col
    return None


class Classification:
    def __init__(self, operation, **params):
        self.operation = operation
        self.params = params


def classify_operation(question, available_columns):
    """
    Returns a Classification for one of:
      "sort", "limit", "dedupe", "column_select", "chart" -- a
      deterministic transform tier-1 may be able to satisfy, or
      "other" -- doesn't match a known deterministic pattern; app.py
      should escalate straight to the LLM-modify tier.

    Order matters: chart/limit/dedupe checks come before the generic
    "only show X" column-select pattern because that phrase overlaps
    with several of them ("only show the first 10" is a limit, not a
    column selection).
    """
    if _CHART_PATTERN.search(question):
        type_match = _CHART_TYPE_PATTERN.search(question)
        # "auto" (not None) when no specific type was named: this is
        # still an explicit request for *a* chart ("chart it", "show me
        # a graph"), so chart_advisor.recommend() should try harder to
        # produce something than it would for an automatic per-query
        # suggestion -- see recommend()'s forced_type docstring.
        return Classification("chart", forced_type=type_match.group(1).lower() if type_match else "auto")

    limit_match = _LIMIT_PATTERN.search(question)
    if limit_match:
        n = int(limit_match.group(1) or limit_match.group(2))
        return Classification("limit", n=n)

    if _DEDUPE_PATTERN.search(question):
        # "remove duplicate emails" -> field text is whatever follows the
        # trigger phrase; may be empty ("just remove duplicates"), which
        # means dedupe on the whole row.
        after = _DEDUPE_PATTERN.split(question, maxsplit=1)[-1].strip()
        field = resolve_column(after, available_columns) if after else None
        return Classification("dedupe", field=field)

    if _SORT_PATTERN.search(question):
        by_match = re.search(r"(?i)\bby\s+([a-zA-Z ]+)", question)
        field_text = by_match.group(1) if by_match else _SORT_PATTERN.split(question, maxsplit=1)[-1]
        field_text = _DIRECTION_WORD_PATTERN.sub("", field_text).strip()
        field = resolve_column(field_text, available_columns)
        return Classification("sort", field=field, descending=bool(_DESC_PATTERN.search(question)))

    column_select_match = _COLUMN_SELECT_PATTERN.match(question)
    if column_select_match:
        requested = re.split(r",|\band\b", column_select_match.group(1))
        resolved = [resolve_column(r, available_columns) for r in requested]
        if all(resolved):
            return Classification("column_select", fields=resolved)
        # Matched the "only show X" shape but X isn't a set of real column
        # names -- fall through rather than giving up here. "Only show
        # active donors" matches this same leading phrase but "active
        # donors" isn't a column list, it's an adjective filter, handled
        # just below.

    # "only show active donors" -- an adjective-based filter, not a
    # column_select. Checked last because it needs the status-adjective
    # vocabulary check to be worth attempting at all.
    lowered = question.lower()
    for adjective, value in _STATUS_ADJECTIVES.items():
        if adjective in lowered:
            status_col = find_status_like_column(available_columns)
            if status_col:
                return Classification("filter", column=status_col, value=value)
            break  # matched an adjective but no status column -- escalate

    return Classification("other")


def apply_transform(rows, classification, columns):
    """
    Executes a Classification against the stored rows. Returns the new
    row list, or None if this particular instance can't be confidently
    applied (e.g. sort field didn't resolve to a real column) -- None
    means "escalate to the LLM tier," never "guess."
    """
    op = classification.operation

    if op == "sort":
        field = classification.params.get("field")
        if not field:
            return None
        descending = classification.params["descending"]

        # Regression guard: a cache hit round-trips MySQL DECIMAL columns
        # through JSON as plain strings ("85000.00"), because Decimal
        # isn't JSON-native (see query_cache.py's json.dumps(default=str)
        # and chart_advisor.py's identical fix for the same reason). Sorting
        # those as strings compares lexicographically -- "85000.00" sorts
        # ahead of "100000.00" because "8" > "1" -- scrambling anything but
        # single-digit-leading values. If every non-null value in this
        # column looks like a numeric string, sort on its float value
        # instead of the raw string.
        non_null = [r.get(field) for r in rows if r.get(field) is not None]
        numeric = bool(non_null) and all(
            isinstance(v, (int, float)) or _looks_numeric_string(v) for v in non_null
        )

        def sort_key(row):
            value = row.get(field)
            if value is None:
                return (True, 0)
            return (False, float(value)) if numeric else (False, value)

        return sorted(rows, key=sort_key, reverse=descending)

    if op == "limit":
        return rows[: classification.params["n"]]

    if op == "dedupe":
        field = classification.params.get("field")
        seen = set()
        deduped = []
        for row in rows:
            key = row.get(field) if field else tuple(sorted(row.items()))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped

    if op == "column_select":
        fields = classification.params["fields"]
        return [{f: row.get(f) for f in fields} for row in rows]

    if op == "filter":
        column, value = classification.params["column"], classification.params["value"]
        if column not in columns:
            return None
        return [row for row in rows if str(row.get(column, "")).lower() == value.lower()]

    return None


def describe_transform(classification):
    """
    Plain-English record of what apply_transform just did, for the
    transform_log persisted in conversation_state.py -- lets the UI (and
    a future LLM-escalation prompt) show an honest trail of what's been
    done to the data without reconstructing SQL for operations that
    don't map onto a single clean SQL clause (e.g. dedupe-by-column,
    which needs a window function to express faithfully).
    """
    op, p = classification.operation, classification.params
    if op == "sort":
        return f"sorted by {p['field']} ({'descending' if p['descending'] else 'ascending'})"
    if op == "limit":
        return f"limited to the first {p['n']} rows"
    if op == "dedupe":
        return f"removed duplicate {p['field']} values" if p.get("field") else "removed duplicate rows"
    if op == "column_select":
        return f"showing only: {', '.join(p['fields'])}"
    if op == "filter":
        return f"filtered to {p['column']} = '{p['value']}'"
    return "transformed"

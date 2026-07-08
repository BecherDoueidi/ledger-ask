"""
Decides whether a SQL result set should be visualized, and if so, which
chart type and how to shape the data for it -- run once per successful
query in app.py (catalog / cache / LLM paths all funnel through
recommend()), regardless of which path produced the data.

Deliberately generic: every rule here looks only at the SHAPE of the
result (column names + inferred types + cardinality), never at the SQL
text or the original question. That's what lets it work for questions
nobody has written a rule for yet -- "donation amount by quarter" and
"visits by branch" hit the same "1 categorical + 1 numeric" rule without
either being special-cased.

Adding a new chart type later: write one function with the signature
`(columns, rows) -> Recommendation | None` and add it to CHART_RULES in
priority order (most specific first). Nothing else needs to change --
recommend() just tries each rule until one matches.
"""

import re
from datetime import date, datetime
from decimal import Decimal

MAX_CATEGORIES_FOR_CHART = 20
PERCENT_NAME_PATTERN = re.compile(r"(?i)percent|\b(pct|share|proportion)\b|%")

_MONTH_NAMES = {
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
}
_TEMPORAL_NAME_SUFFIXES = ("date", "year", "month", "quarter", "week")
_TEMPORAL_NAME_EXACT = {"day", "period"}


def _looks_temporal_by_name(name):
    """
    Matches both standalone words ("Date", "Quarter") and the far more
    common SQL alias style produced by GROUP BY expressions, where the
    unit is a suffix on a compound identifier with no word boundary
    (DonationMonth, DonationYear, FiscalQuarter) -- a plain \\b-anchored
    regex misses all of those since there's no non-word character
    between "Donation" and "Month" for \\b to match against.
    """
    lowered = name.lower()
    if lowered in _TEMPORAL_NAME_EXACT:
        return True
    return any(lowered.endswith(suffix) for suffix in _TEMPORAL_NAME_SUFFIXES)


class Recommendation:
    def __init__(self, chart_type, labels, datasets, title=None, x_label=None, y_label=None):
        self.chart_type = chart_type
        self.labels = labels
        self.datasets = datasets
        self.title = title
        self.x_label = x_label
        self.y_label = y_label

    def to_dict(self):
        return {
            "chart_type": self.chart_type,
            "labels": self.labels,
            "datasets": self.datasets,
            "title": self.title,
            "x_label": self.x_label,
            "y_label": self.y_label,
        }


_NUMERIC_STRING_PATTERN = re.compile(r"^-?\d+\.\d+$")


def _is_numeric(value):
    """
    True for real numeric types, but ALSO for strings that look like a
    cached DECIMAL value (e.g. "100000.00"). This matters because cached
    results have round-tripped through JSON: MySQL DECIMAL columns arrive
    as Decimal from a fresh SQLAlchemy execution, but query_cache.py
    necessarily serializes them with json.dumps(default=str) to store
    them (Decimal isn't JSON-native) and get back plain strings on a
    cache hit. Without this, every cached money column would silently
    stop being chartable while the exact same fresh query still worked.

    The decimal point is REQUIRED, deliberately -- this was initially
    "any all-digit string," which misclassified phone numbers, zip
    codes, and other digit-string identifiers (e.g. Mobile "0501112223")
    as a numeric/measurement column, producing a nonsensical forced
    chart like "Mobile by FullName". Real ints (COUNT(*), plain INTEGER
    columns) are JSON-native and never go through this string path at
    all -- only Decimal does, and every DECIMAL column in this schema is
    declared with a fixed scale (e.g. DECIMAL(18,2)), so it always
    round-trips with a "." present. An all-digit string with no decimal
    point reaching this function is therefore never a genuine cached
    measurement, only ever an identifier-shaped value.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    if isinstance(value, str):
        return bool(_NUMERIC_STRING_PATTERN.match(value.strip()))
    return False


def _is_temporal_value(value):
    if isinstance(value, (date, datetime)):
        return True
    if isinstance(value, str):
        lowered = value.strip().lower()
        if re.match(r"^\d{4}-\d{2}(-\d{2})?$", lowered):  # 2026-07 / 2026-07-07
            return True
        if re.match(r"^q[1-4][\s\-]?\d{0,4}$", lowered):  # Q1, Q1-2026
            return True
        if any(lowered.startswith(m) for m in _MONTH_NAMES):
            return True
    return False


def _classify_column(name, values):
    """
    Name-based temporal hints are checked BEFORE the numeric check, not
    after: a GROUP BY YEAR(x)/MONTH(x) query returns plain integers
    (2025, 3, 6, 9...) in columns named e.g. DonationYear/DonationMonth --
    every value passes _is_numeric, so if the numeric check ran first
    these would always be classified "numeric" and the name hint would
    never even be consulted. Checking the name first is what lets a
    "year decomposed into an int column" case still register as
    temporal instead of being indistinguishable from an arbitrary count.
    """
    non_null = [v for v in values if v is not None]
    if not non_null:
        return "categorical"
    if _looks_temporal_by_name(name):
        return "temporal"
    if all(_is_numeric(v) for v in non_null):
        return "numeric"
    if all(_is_temporal_value(v) for v in non_null):
        return "temporal"
    return "categorical"


def _describe_columns(rows):
    """Returns [{"name", "kind", "cardinality"}] in original column order."""
    if not rows:
        return []
    columns = list(rows[0].keys())
    described = []
    for name in columns:
        values = [row.get(name) for row in rows]
        kind = _classify_column(name, values)
        described.append({
            "name": name,
            "kind": kind,
            "cardinality": len(set(values)),
            "values": values,
        })
    return described


def _looks_like_percentages(numeric_col):
    if PERCENT_NAME_PATTERN.search(numeric_col["name"]):
        return True
    total = sum(float(v) for v in numeric_col["values"] if v is not None)
    return 95 <= total <= 105


PALETTE = ["#C4621A", "#2C5282", "#2F6B4F", "#A33A2E", "#6B4C9A", "#1F7A8C", "#B8860B", "#7A5C3E"]


def _color(i):
    return PALETTE[i % len(PALETTE)]


def _rule_time_series(columns, rows):
    """
    1+ temporal columns and exactly 1 numeric column, with nothing else
    in the result. More than one temporal column is deliberately
    supported: a "totals by month" question very commonly comes back as
    separate YEAR(...)/MONTH(...) columns rather than one formatted date
    string, so this joins whichever temporal columns are present into a
    single composite x-axis label (e.g. "2025-3") instead of requiring
    exactly one.
    """
    temporal = [c for c in columns if c["kind"] == "temporal"]
    numeric = [c for c in columns if c["kind"] == "numeric"]
    if not temporal or len(numeric) != 1 or len(temporal) + len(numeric) != len(columns):
        return None
    if len(rows) <= 2:
        return None

    n_col = numeric[0]
    labels = [
        "-".join(str(t["values"][i]) if t["values"][i] is not None else "?" for t in temporal)
        for i in range(len(rows))
    ]
    axis_name = "/".join(t["name"] for t in temporal)
    return Recommendation(
        chart_type="line",
        labels=labels,
        datasets=[{
            "label": n_col["name"],
            "data": [float(v) if v is not None else None for v in n_col["values"]],
            "borderColor": _color(0),
            "backgroundColor": _color(0),
        }],
        title=f"{n_col['name']} over {axis_name}",
        x_label=axis_name, y_label=n_col["name"],
    )


def _rule_category_bar_or_pie(columns, rows):
    if len(columns) != 2:
        return None
    categorical = [c for c in columns if c["kind"] == "categorical"]
    numeric = [c for c in columns if c["kind"] == "numeric"]
    if len(categorical) != 1 or len(numeric) != 1:
        return None
    cat_col, n_col = categorical[0], numeric[0]
    if cat_col["cardinality"] > MAX_CATEGORIES_FOR_CHART:
        return None

    labels = [str(v) if v is not None else "(none)" for v in cat_col["values"]]
    values = [float(v) if v is not None else 0 for v in n_col["values"]]

    if _looks_like_percentages(n_col) and cat_col["cardinality"] <= 12:
        return Recommendation(
            chart_type="pie",
            labels=labels,
            datasets=[{"label": n_col["name"], "data": values,
                       "backgroundColor": [_color(i) for i in range(len(labels))]}],
            title=f"{n_col['name']} by {cat_col['name']}",
        )

    return Recommendation(
        chart_type="bar",
        labels=labels,
        datasets=[{"label": n_col["name"], "data": values, "backgroundColor": _color(0)}],
        title=f"{n_col['name']} by {cat_col['name']}",
        x_label=cat_col["name"], y_label=n_col["name"],
    )


def _rule_scatter(columns, rows):
    if len(columns) != 2:
        return None
    numeric = [c for c in columns if c["kind"] == "numeric"]
    if len(numeric) != 2:
        return None
    x_col, y_col = numeric[0], numeric[1]
    points = [
        {"x": float(x) if x is not None else None, "y": float(y) if y is not None else None}
        for x, y in zip(x_col["values"], y_col["values"])
    ]
    return Recommendation(
        chart_type="scatter",
        labels=None,
        datasets=[{"label": f"{y_col['name']} vs {x_col['name']}", "data": points,
                   "backgroundColor": _color(0)}],
        title=f"{y_col['name']} vs {x_col['name']}",
        x_label=x_col["name"], y_label=y_col["name"],
    )


def _rule_multi_series_bar_or_line(columns, rows):
    if len(columns) < 3:
        return None
    grouping = [c for c in columns if c["kind"] in ("categorical", "temporal")]
    numeric = [c for c in columns if c["kind"] == "numeric"]
    if len(grouping) != 1 or len(numeric) < 2:
        return None
    group_col = grouping[0]
    if group_col["cardinality"] > MAX_CATEGORIES_FOR_CHART:
        return None

    labels = [str(v) if v is not None else "(none)" for v in group_col["values"]]
    datasets = [
        {
            "label": n_col["name"],
            "data": [float(v) if v is not None else None for v in n_col["values"]],
            "backgroundColor": _color(i), "borderColor": _color(i),
        }
        for i, n_col in enumerate(numeric)
    ]
    chart_type = "line" if group_col["kind"] == "temporal" and len(rows) > 2 else "bar"
    return Recommendation(
        chart_type=chart_type, labels=labels, datasets=datasets,
        title=f"{', '.join(c['name'] for c in numeric)} by {group_col['name']}",
        x_label=group_col["name"],
    )


# Order matters: most specific/confident rules first.
CHART_RULES = [
    _rule_time_series,
    _rule_scatter,
    _rule_category_bar_or_pie,
    _rule_multi_series_bar_or_line,
]


_FORCED_TYPE_ALIASES = {"doughnut": "pie"}


def _force_type(columns, rows, forced_type):
    """
    Best-effort construction for an EXPLICITLY requested chart type
    ("create a bar chart" as a follow-up -- see followup_resolver.py),
    bypassing the normal rules' confidence gates (cardinality caps,
    minimum row counts). A user who names a specific chart type has
    already made the judgment call that a chart belongs here; declining
    to draw ANYTHING would be worse than a best-effort rendering, so this
    relaxes the guards rather than reusing CHART_RULES as-is.
    """
    forced_type = _FORCED_TYPE_ALIASES.get(forced_type, forced_type)

    if forced_type == "scatter":
        numeric = [c for c in columns if c["kind"] == "numeric"]
        if len(numeric) < 2:
            return None
        x_col, y_col = numeric[0], numeric[1]
        points = [
            {"x": float(x) if x is not None else None, "y": float(y) if y is not None else None}
            for x, y in zip(x_col["values"], y_col["values"])
        ]
        return Recommendation(
            chart_type="scatter", labels=None,
            datasets=[{"label": f"{y_col['name']} vs {x_col['name']}", "data": points,
                       "backgroundColor": _color(0)}],
            title=f"{y_col['name']} vs {x_col['name']}", x_label=x_col["name"], y_label=y_col["name"],
        )

    if forced_type not in ("bar", "line", "pie"):
        return None

    grouping = [c for c in columns if c["kind"] in ("categorical", "temporal")]
    numeric = [c for c in columns if c["kind"] == "numeric"]
    if grouping and numeric:
        n_col = numeric[0]
        axis_name = " / ".join(g["name"] for g in grouping)
        labels = _composite_labels(grouping, len(rows))
    elif numeric:
        # No categorical/temporal axis at all (e.g. a single numeric
        # column) -- fall back to row position as the label so a forced
        # chart still renders something rather than nothing.
        n_col = numeric[0]
        axis_name = None
        labels = [f"Row {i + 1}" for i in range(len(rows))]
    else:
        return None

    values = [float(v) if v is not None else 0 for v in n_col["values"]]
    if forced_type == "pie":
        dataset = {"label": n_col["name"], "data": values,
                   "backgroundColor": [_color(i) for i in range(len(labels))]}
    else:
        dataset = {"label": n_col["name"], "data": values, "backgroundColor": _color(0), "borderColor": _color(0)}

    return Recommendation(
        chart_type=forced_type, labels=labels, datasets=[dataset],
        title=f"{n_col['name']}" + (f" by {axis_name}" if axis_name else ""),
        x_label=axis_name,
        y_label=n_col["name"] if forced_type != "pie" else None,
    )


def _composite_labels(grouping_columns, row_count):
    """
    Joins every categorical/temporal column's value per-row into one
    axis label ("Ahmed Ali · Q1 2024") instead of picking just the first
    grouping column and silently dropping the rest -- a dataset grouped
    by donor AND year AND quarter has three dimensions worth showing,
    not one.
    """
    return [
        " · ".join(
            str(g["values"][i]) if g["values"][i] is not None else "?"
            for g in grouping_columns
        )
        for i in range(row_count)
    ]


def _force_generic(columns, rows):
    """
    Best-effort chart for an explicit-but-untyped request ("chart it",
    "show me a graph") when the normal confidence-gated CHART_RULES all
    declined -- typically because the result has MORE than one
    categorical/temporal dimension (e.g. donor x year x quarter), which
    every strict single-dimension rule requires exactly one of. Silence
    is the right response to an automatic per-query suggestion in that
    case, but not to a direct request -- so this combines every
    grouping column into one composite axis instead, and still declines
    only if there's truly no numeric measure to plot at all.
    """
    grouping = [c for c in columns if c["kind"] in ("categorical", "temporal")]
    numeric = [c for c in columns if c["kind"] == "numeric"]
    if not grouping or not numeric:
        return None

    n_col = numeric[0]
    # Always bar here, never line: _rule_time_series (earlier in
    # CHART_RULES) already handles the clean "purely temporal, single
    # measure" case with a line chart. By the time control reaches this
    # fallback, grouping has MORE than one dimension and it's almost
    # always a mix of categorical + temporal (e.g. donor x quarter) --
    # a line implies continuous progression along the x-axis, which is
    # misleading when the axis is really "one bar per unique combination"
    # ordered by donor first, not by time.
    chart_type = "bar"
    axis_name = " / ".join(g["name"] for g in grouping)
    return Recommendation(
        chart_type=chart_type,
        labels=_composite_labels(grouping, len(rows)),
        datasets=[{
            "label": n_col["name"],
            "data": [float(v) if v is not None else None for v in n_col["values"]],
            "backgroundColor": _color(0), "borderColor": _color(0),
        }],
        title=f"{n_col['name']} by {axis_name}",
        x_label=axis_name, y_label=n_col["name"],
    )


def recommend(rows, forced_type=None):
    """
    rows: the list[dict] result set (as returned by SQLAlchemy's
    .mappings()), or None/[] for non-SELECT / empty results.
    forced_type:
        None      -- fully automatic (used for every catalog/cache/LLM
                     response, whether or not the user asked for a
                     chart). Conservative by design: Phase 2's "do not
                     generate charts for every query" means declining on
                     an ambiguous/multi-dimensional shape is correct here.
        "auto"    -- the user explicitly asked for *a* chart but didn't
                     name a type ("chart it", "show me a graph" -- see
                     followup_resolver.py). Tries the normal rules first,
                     but if they all decline (e.g. more than one
                     categorical/temporal dimension), falls back to
                     _force_generic rather than showing nothing --
                     silence is the wrong response to a direct request,
                     even though it's the right response to automatic
                     per-query suggestion.
        "bar" | "line" | "pie" | "scatter" | "doughnut" -- the user named
                     a specific type; see _force_type.
    Returns a JSON-serializable dict (Chart.js-shaped) or None if no
    visualization is appropriate for this result.
    """
    if not rows:
        return None
    columns = _describe_columns(rows)
    if len(columns) < 1:
        return None

    if forced_type == "auto":
        for rule in CHART_RULES:
            recommendation = rule(columns, rows)
            if recommendation is not None:
                return recommendation.to_dict()
        recommendation = _force_generic(columns, rows)
        return recommendation.to_dict() if recommendation is not None else None

    if forced_type:
        recommendation = _force_type(columns, rows, forced_type)
        if recommendation is not None:
            return recommendation.to_dict()
        # Requested type genuinely doesn't fit this data (e.g. "scatter"
        # with only one numeric column) -- fall through to the normal
        # advisor rather than showing nothing.

    if len(columns) < 2:
        return None
    for rule in CHART_RULES:
        recommendation = rule(columns, rows)
        if recommendation is not None:
            return recommendation.to_dict()
    return None

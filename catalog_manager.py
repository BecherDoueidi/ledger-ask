"""
Reads/writes catalog.yaml -- the "Path 3: Deterministic Compiler" bypass
described in the README. Questions whose exact wording matches a promoted
entry skip the LLM entirely: the stored SQL is run live against the
database (so the data is always fresh) with zero model inference cost.
"""

import yaml
import os

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.yaml")


def _normalize(text_value):
    return " ".join(text_value.strip().lower().split())


def _load():
    if not os.path.exists(CATALOG_PATH):
        return {"promoted_queries": []}
    with open(CATALOG_PATH, "r") as f:
        data = yaml.safe_load(f) or {}
    if not data.get("promoted_queries"):
        data["promoted_queries"] = []
    return data


def find_match(user_query):
    """Return the promoted SQL string if this question matches a catalog
    entry (case/whitespace-insensitive), else None."""
    data = _load()
    target = _normalize(user_query)
    for entry in data.get("promoted_queries", []):
        if _normalize(entry.get("intent", "")) == target:
            return entry.get("sql")
    return None


def _catalog_file_needs_header():
    """
    True if catalog.yaml doesn't exist yet, or exists but is empty/
    whitespace-only -- in either case there's no "promoted_queries:" key
    for the indented list items promote() appends to hang off of.
    """
    if not os.path.exists(CATALOG_PATH):
        return True
    with open(CATALOG_PATH, "r") as f:
        return not f.read().strip()


def promote(intent, sql):
    """
    Append a new promoted_queries entry to catalog.yaml.
    Appends as text (rather than re-dumping the whole YAML file) so the
    existing header comments in catalog.yaml are preserved.

    `intent` must be the raw question text with no role/donor prefix
    baked in, and `sql` must not depend on any one person's row-level
    scope. The catalog is a GLOBAL, unrestricted shortcut -- anything
    promoted into it runs verbatim, with no access-control re-check, for
    whoever's question text matches it later. That's why app.py's
    promote_entry route only allows promoting entries that were
    originally asked by an admin (unrestricted role) in the first place.

    If catalog.yaml doesn't exist yet (fresh deployment, or it was
    deleted), this writes the "promoted_queries:" header first --
    without it, this function would append bare "  - intent: ..." list
    items with no parent key, which yaml.safe_load() parses as a raw
    list rather than a dict, and _load()'s data.get("promoted_queries")
    would then crash on every future call, breaking the catalog lookup
    entirely (not just failing to find the new entry).
    """
    if _catalog_file_needs_header():
        with open(CATALOG_PATH, "w") as f:
            f.write("promoted_queries:\n")

    block = yaml.safe_dump(
        [{"intent": intent, "sql": sql}], default_flow_style=False, sort_keys=False
    )
    indented = "\n".join(
        ("  " + line if line.strip() else line) for line in block.splitlines()
    )
    with open(CATALOG_PATH, "a") as f:
        f.write("\n" + indented + "\n")

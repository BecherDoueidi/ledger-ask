import hashlib

from sqlalchemy import inspect
from db_config import engine


def discover_row_scoped_tables(column_name):
    """
    Returns every live table that has a column named column_name
    (case-insensitive). This is what lets a row-restricted role (e.g.
    "donor") stay correct across a database swap without anyone editing
    roles_config.py: instead of a hand-maintained table list, the set of
    tables that role can touch is *derived* from which tables actually
    carry the ownership column right now. Rename "Donations" to
    "Contributions" and it's still found, as long as it still has a
    DonorId column; add a brand-new table with a DonorId column and it's
    automatically included too.

    This can't discover an ownership convention it isn't told about --
    if the new database scopes rows by a completely different column
    name, row_filter_column in roles_config.py still needs updating to
    match. What it removes is the need to also keep an exhaustive table
    list in sync by hand.
    """
    inspector = inspect(engine)
    column_name = column_name.lower()
    matches = []
    for table_name in inspector.get_table_names():
        columns = {col["name"].lower() for col in inspector.get_columns(table_name)}
        if column_name in columns:
            matches.append(table_name)
    return matches


def extract_live_metadata(allowed_tables=None):
    """
    Reads the live schema from the shared `engine` (configured in
    db_config.py) and returns (dialect_name, schema_text).

    allowed_tables: if provided, only these tables are included -- the
    LLM is never even told the other tables exist. None means no
    restriction (every table is included).
    """
    inspector = inspect(engine)
    db_dialect = engine.dialect.name

    all_tables = inspector.get_table_names()
    if allowed_tables is not None:
        allowed_set = {t.lower() for t in allowed_tables}
        table_names = [t for t in all_tables if t.lower() in allowed_set]
    else:
        table_names = all_tables

    schema_text = ""
    for table_name in table_names:
        schema_text += f"Table: {table_name}\n"
        # NOTE: this loop must stay indented under the table_name loop --
        # it used to sit at the wrong indentation level and only ever
        # harvested columns for the last table in the database.
        for column in inspector.get_columns(table_name):
            schema_text += f"  - {column['name']} ({str(column['type'])})\n"

    return db_dialect, schema_text


def compute_schema_fingerprint(schema_text):
    """
    Cheap, deterministic hash of a schema_text string (as returned by
    extract_live_metadata). Compared against a stored fingerprint on a
    cache lookup (see query_cache.py / app.py) to detect that the live
    database's shape has changed since an entry was cached -- e.g. the
    database was swapped, a table was renamed, or a column was added or
    removed -- so a stale answer never gets silently replayed against a
    schema it was never generated for. A plain function (not bundled
    into extract_live_metadata) so it can be unit-tested without a live
    database connection.
    """
    return hashlib.sha256(schema_text.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    print("Extracting live metadata...\n")
    print(extract_live_metadata())

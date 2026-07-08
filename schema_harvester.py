from sqlalchemy import inspect
from db_config import engine


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


if __name__ == "__main__":
    print("Extracting live metadata...\n")
    print(extract_live_metadata())

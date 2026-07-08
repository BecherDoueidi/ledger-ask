"""
Centralized database configuration.

Both app.py and schema_harvester.py import `engine` from here so there is
a single source of truth for the connection — previously each file had its
own hardcoded connection string, which is a drift risk (fix the password
in one place, forget the other).

Credentials are read from environment variables / a .env file rather than
hardcoded. See .env.example for the variables you need to set.

Swapping databases: everything else in this codebase (schema harvesting,
role-based table/row scoping, the LLM prompt's dialect rules) already
adapts to whatever's live at request time -- see schema_harvester.py and
roles_config.py. The one thing that's inherently connection-specific is
this file. Two ways to point at a different database:

  - Same engine (MySQL), different host/credentials/name: just change
    DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME in .env. Nothing else to
    touch.
  - A different DBMS entirely (Postgres, SQL Server, SQLite, ...): set
    DATABASE_URL in .env to a full SQLAlchemy connection URL, e.g.
    postgresql+psycopg2://user:pass@host:5432/dbname. This takes
    precedence over the individual DB_* vars below. You'll need the
    matching driver package installed (e.g. psycopg2-binary for
    Postgres) -- that's the one piece this can't auto-install for you.
    The LLM prompt already has explicit dialect rules for sqlite/mysql
    (see app.py's build_system_prompt); other dialects still work --
    SQLAlchemy reports the real dialect name and it's told to the
    model -- they just don't get dialect-specific few-shot examples yet.
"""

import os
from sqlalchemy import create_engine

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv not installed -- fall back to whatever is already
    # in the real environment variables (e.g. set by Docker / the OS).
    pass

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "")

CONNECTION_STRING = os.getenv("DATABASE_URL") or (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# pool_pre_ping checks a connection is still alive before using it, which
# matters for MySQL: idle connections get silently dropped by the server
# after a timeout, causing "MySQL server has gone away" errors otherwise.
engine = create_engine(CONNECTION_STRING, pool_pre_ping=True)

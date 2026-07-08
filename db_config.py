"""
Centralized database configuration.

Both app.py and schema_harvester.py import `engine` from here so there is
a single source of truth for the connection — previously each file had its
own hardcoded connection string, which is a drift risk (fix the password
in one place, forget the other).

Credentials are read from environment variables / a .env file rather than
hardcoded. See .env.example for the variables you need to set.
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

CONNECTION_STRING = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# pool_pre_ping checks a connection is still alive before using it, which
# matters for MySQL: idle connections get silently dropped by the server
# after a timeout, causing "MySQL server has gone away" errors otherwise.
engine = create_engine(CONNECTION_STRING, pool_pre_ping=True)

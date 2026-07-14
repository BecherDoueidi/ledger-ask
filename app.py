"""
Application entry point. Everything that used to live here directly --
route handlers, the LLM client, prompt building, the SQL-safety guards,
and the full /api/generate-sql pipeline -- now lives in dedicated
modules (routes/, llm_client.py, prompt_builder.py, sql_safety.py,
query_service.py). This file's only job is to assemble them: create the
Flask app, configure the session secret, seed default accounts, and
register every blueprint.
"""

import os
import secrets
import logging

from flask import Flask

import auth
from logging_config import configure_logging
from routes import register_blueprints

configure_logging()
logger = logging.getLogger(__name__)

app = Flask(__name__)

# SECRET_KEY signs the session cookie. Without a stable key, every
# restart would invalidate all logged-in sessions; a hardcoded key
# would let anyone who reads this source forge sessions. Read it from
# the environment (see .env.example) and only fall back to a random
# throwaway key -- with a loud warning -- if it's missing, so a forgotten
# .env entry fails safe (sessions just don't survive a restart) instead
# of silently shipping a guessable secret.
_secret = os.getenv("SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
    logger.warning(
        "SECRET_KEY not set in environment -- using a random key for this run only "
        "(all sessions will be invalidated on restart). Set SECRET_KEY in your .env "
        "for production use."
    )
app.secret_key = _secret

# Idempotent: only creates the seeded accounts if users.db is empty, so
# this is safe to run on every startup without overwriting real
# passwords an admin has since changed. See auth.py.
auth.seed_default_users()

register_blueprints(app)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.getenv('PORT', 5000)), debug=True)

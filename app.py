import os
import re
import secrets
import sqlite3
from functools import wraps
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
# The ghost import is gone. We only import the correct, live harvester.
from schema_harvester import extract_live_metadata, compute_schema_fingerprint
from openai import OpenAI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

# Shared MySQL engine (credentials come from env vars / .env -- see db_config.py)
from db_config import engine
import query_cache
import catalog_manager
import staging_queue
import roles_config
import access_control
import auth
import query_analytics
import chart_advisor
import conversation_state
import followup_resolver

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
    print("WARNING: SECRET_KEY not set in environment. Using a random key "
          "for this run only -- all sessions will be invalidated on restart. "
          "Set SECRET_KEY in your .env for production use.")
app.secret_key = _secret

# Idempotent: only creates the seeded accounts if users.db is empty, so
# this is safe to run on every startup without overwriting real
# passwords an admin has since changed. See auth.py.
auth.seed_default_users()


def login_required(view):
    """
    Redirects anonymous browser requests to /login, but returns a JSON
    401 for API calls -- a JSON fetch() following an HTML redirect just
    gets tangled up in the frontend instead of surfacing the real
    problem, so API routes need their own explicit signal.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            if request.path.startswith("/api/"):
                return jsonify({
                    "status": "error",
                    "error_code": "NOT_AUTHENTICATED",
                    "message": "You must be logged in to do this."
                }), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def capability_required(capability):
    """
    Same as login_required, but also requires the session's role to have
    the given can_* flag set in roles_config.ROLES (e.g.
    "can_manage_users", "can_promote_to_catalog"). This is what replaced
    a single blanket "admin" check -- admin was split into three tiers
    (viewer/analyst/admin, see roles_config.py) with different subsets
    of these capabilities, so each route now names the specific
    capability it actually requires instead of asking "is this an
    admin?".
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "username" not in session:
                if request.path.startswith("/api/"):
                    return jsonify({
                        "status": "error",
                        "error_code": "NOT_AUTHENTICATED",
                        "message": "You must be logged in to do this."
                    }), 401
                return redirect(url_for("login"))
            if not roles_config.has_capability(session.get("role"), capability):
                if request.path.startswith("/api/"):
                    return jsonify({
                        "status": "error",
                        "error_code": "ACCESS_DENIED",
                        "message": "You don't have permission to do this."
                    }), 403
                return redirect(url_for("index"))
            return view(*args, **kwargs)
        return wrapped
    return decorator
# Hijack the OpenAI client to point to your local Ollama daemon
client = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="local-bypass" # The library requires a string here, but Ollama ignores it
)

# qwen2.5-coder:14b, not a general chat model, because this task is
# text-to-SQL specifically. A smaller general-purpose model (llama3.2:3b
# was tried here previously) can hallucinate a column and then -- despite
# the self-healing retry loop's explicit "this column does not exist"
# correction -- regenerate the exact same invalid column reference on
# every retry, because it's not strong enough at code/SQL reasoning to
# use the correction. Override via LLM_MODEL if you've pulled a
# different model and verified it against this schema.
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder:14b")


def call_llm_api(system_prompt, user_query, temperature=0.0):
    """
    Executes a live inference call to the local Ollama engine.

    temperature=0.0 is used for the first attempt (deterministic, most
    reliable when it's right). On retries, a small amount of temperature
    is used instead -- at temperature=0.0 the model is fully
    deterministic, so if its first guess contained a reasoning mistake
    (e.g. attributing a column to the wrong table), every "self-healing"
    retry was observed to regenerate the exact same wrong SQL verbatim,
    guaranteeing failure even though the error message correctly
    described the problem. A little randomness lets each retry actually
    have a chance to reconsider instead of robotically repeating itself.
    """
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query}
        ],
        temperature=temperature
    )
    return response.choices[0].message.content

def print_results_table(data):
    """
    Pretty-prints a list of row-dicts as an aligned ASCII table in the
    terminal, instead of the default ugly single-line list-of-dicts.
    No external dependency (no tabulate/pandas needed).
    """
    if not data:
        print("(query succeeded, 0 rows returned)")
        return

    columns = list(data[0].keys())

    # Compute each column's display width: the widest of the header
    # itself or any value in that column (values are str()'d first,
    # with None shown as "NULL" so it's visually distinct from "None").
    col_widths = {}
    for col in columns:
        values = ["NULL" if row.get(col) is None else str(row.get(col)) for row in data]
        col_widths[col] = max(len(col), *(len(v) for v in values)) if values else len(col)

    def format_row(values):
        return " | ".join(str(v).ljust(col_widths[col]) for col, v in zip(columns, values))

    separator = "-+-".join("-" * col_widths[col] for col in columns)

    print(format_row(columns))
    print(separator)
    for row in data:
        row_values = ["NULL" if row.get(col) is None else row.get(col) for col in columns]
        print(format_row(row_values))
    print(f"({len(data)} row{'s' if len(data) != 1 else ''})")


_UNKNOWN_COLUMN_PATTERN = re.compile(r"(?i)unknown column '(?:([A-Za-z_]\w*)\.)?(\w+)'")
_ALIAS_TABLE_PATTERN = re.compile(r'(?i)\b(?:FROM|JOIN)\s+`?(\w+)`?(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?')


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
        # hard-constraint mechanism in app.py (which doesn't require
        # guessing intent) is what actually prevents this from looping.
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


_SQL_LEADING_VERB_PATTERN = re.compile(r"(?i)^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b")


_MARKDOWN_FENCE_PATTERN = re.compile(r"^```(?:sql)?\s*\n?|\n?```$", re.IGNORECASE)


def strip_markdown_fence(text_output):
    """
    Strips a leading/trailing ```sql ... ``` (or bare ``` ... ```) code
    fence if the model wrapped its answer in one, e.g.:

        ```sql
        SELECT ...
        ```

    instead of the requested bare SQL string. Observed in practice on
    the follow-up-modify path (see build_system_prompt's
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


def build_system_prompt(db_dialect, live_schema, allowed_tables, donor_id, row_filter_column=None, followup_context=None):
    """
    Builds the text-to-SQL system prompt: dialect rules, few-shot syntax
    anchors, the role-scoping addendum, and the live schema. Shared by
    BOTH a fresh question and an LLM-assisted follow-up modification
    (see followup_resolver.py / the conversation-follow-up branch in
    generate_sql) so the dialect/security/schema rules never drift
    between the two call sites -- only followup_context differs.

    followup_context: {"last_question", "last_sql", "transform_log"} when
    this is tier-2 of a follow-up (the deterministic transform tier in
    followup_resolver.py couldn't satisfy it, e.g. it needs a new JOIN).
    None for a fresh, self-contained question -- today's original prompt,
    byte-for-byte.
    """
    few_shot_examples = ""
    banned_syntax_note = ""
    if db_dialect.lower() == "sqlite":
        banned_syntax_note = "Banned syntax includes TOP clauses, CONCAT functions, and YEAR() calls."
        few_shot_examples = """
### CORRECT SYNTAX EXAMPLES FOR SQLITE:
User Query: Combine first and last names of the top 2 oldest employees and get their hire year.
Correct Response: SELECT FirstName || ' ' || LastName AS FullName, strftime('%Y', HireDate) AS HireYear FROM Employee ORDER BY BirthDate ASC LIMIT 2

User Query: Concatenate city and country for customers and limit to 5.
Correct Response: SELECT BillingCity || ', ' || BillingCountry FROM Invoice LIMIT 5
"""
    elif db_dialect.lower() == "mysql":
        # Banning TOP is still correct for MySQL (it doesn't exist here),
        # but CONCAT() and YEAR() are the CORRECT MySQL syntax -- they
        # must never be banned for this dialect, only for SQLite where
        # || and strftime() are used instead.
        banned_syntax_note = "Banned syntax includes TOP clauses. Use CONCAT() for string concatenation and YEAR() for extracting a year -- these are correct and required for this dialect."
        few_shot_examples = """
### CORRECT SYNTAX EXAMPLES FOR MYSQL:
User Query: Combine first and last name of the top 2 oldest employees and get their hire year.
Correct Response: SELECT CONCAT(FirstName, ' ', LastName) AS FullName, YEAR(HireDate) AS HireYear FROM Employee ORDER BY BirthDate ASC LIMIT 2

User Query: Show total contribution per corporate donor, top 5.
Correct Response: SELECT t1.CompanyName, SUM(t2.DonationAmount) AS TotalContribution FROM CorporateDonors t1 INNER JOIN Donations t2 ON t1.CorporateId = t2.CorporateId GROUP BY t1.CompanyName ORDER BY TotalContribution DESC LIMIT 5
# Note: a monetary-sounding column that exists on the donor/entity table itself (e.g. AnnualContribution)
# is a separate stored figure, NOT the same as summing transactional rows in a related table
# (e.g. Donations.DonationAmount). Never substitute one for the other -- check which table
# a column actually belongs to in the schema below before referencing it, especially across a JOIN.
"""

    # Strict System Boundary Construction -- a role-specific addendum so
    # the LLM knows its SQL will be wrapped in a subquery filter. Without
    # this it writes SUM(d.col) with a table alias that no longer exists
    # after the rewrite, which produces NULL from aggregate functions
    # like SUM/COUNT/AVG.
    role_context = ""
    if allowed_tables is not None:
        role_context = f"""
ROLE RESTRICTION NOTICE: Your SQL will be executed inside a pre-filtered subquery.
Each table reference is automatically rewritten to:
  FROM (SELECT * FROM <table> WHERE {row_filter_column} = {donor_id}) AS <table>
Because of this you MUST follow these rules:
- NEVER use a table alias in aggregate functions. Write SUM(DonationAmount), NOT SUM(d.DonationAmount).
- NEVER prefix column names with a table name. Write DonationAmount, NOT Donations.DonationAmount.
- The filter is already applied -- do NOT add your own WHERE {row_filter_column} = ... clause.
"""

    conversation_context = ""
    if followup_context is not None:
        transform_note = ""
        if followup_context["transform_log"]:
            transform_note = (
                "Since that query ran, the following display-only transforms were applied on top "
                "of its result (these are NOT part of the SQL -- fold their intent into your new "
                "query if the follow-up implies keeping them, e.g. a still-relevant sort or filter): "
                + "; ".join(followup_context["transform_log"]) + ".\n"
            )
        conversation_context = f"""
CONVERSATION CONTEXT: The next user message is a FOLLOW-UP in an ongoing
conversation, not a standalone question.
The previous question was: "{followup_context['last_question']}"
It was answered with this SQL: {followup_context['last_sql']}
{transform_note}Write ONE complete, standalone SQL query that satisfies the follow-up in light of
that context -- extend, modify, or replace the previous query as needed. Do not describe a
diff; return the full query.
"""

    system_prompt = f"""You are an enterprise-grade Text-to-SQL compilation engine.
Your sole mandate is to convert natural language queries into valid, optimized SQL statements.

CRITICAL OPERATIONAL BOUNDARIES:
1. TARGET DIALECT: You are generating SQL for a '{db_dialect.upper()}' database. You MUST write strictly valid {db_dialect.upper()} syntax.
2. Follow the exact formatting patterns demonstrated in the examples below. {banned_syntax_note}
3. NEVER append a semicolon (;) to the end of your generated SQL string.
4. Before referencing any column, verify in the schema below which table it actually belongs to -- do not assume a column exists on a table just because it's semantically related (e.g. a summary/aggregate column stored on one table is not interchangeable with a transactional column on a related table).
5. MINIMALITY: Select ONLY the columns strictly required to answer the question. Do not add extra columns (names, emails, IDs, etc.) or extra JOINs "for context" unless the user explicitly asked for them. A question asking for a single total, count, or average should produce a single-column (or single-value) result -- e.g. "How much have I donated in total?" is exactly `SELECT SUM(DonationAmount) FROM Donations`, nothing more. Every extra column or JOIN you add is a chance to reference a column that doesn't exist.
{role_context}
{few_shot_examples}
{conversation_context}
Target Database Schema Context:
{live_schema}
"""
    return system_prompt


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login form (GET) and credential check (POST)."""
    if request.method == 'GET':
        if "username" in session:
            return redirect(url_for('index'))
        return render_template('login.html', error=None)

    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    user = auth.verify_user(username, password)
    if user is None:
        return render_template('login.html', error="Incorrect username or password."), 401

    session['username'] = user['username']
    session['role'] = user['role']
    session['donor_id'] = user['donor_id']
    return redirect(url_for('index'))


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    """User-facing page: a plain-English question box."""
    role_name = session.get('role')
    role = roles_config.get_role(role_name) or {}
    return render_template(
        'index.html',
        username=session.get('username'),
        role=role_name,
        can_view_admin_panel=role.get('can_view_admin_panel', False),
        is_self_service=role.get('row_filter_column') is not None,
    )


@app.route('/admin')
@capability_required("can_view_admin_panel")
def admin():
    """Admin panel: staging queue, analytics visibility, and (admin-tier only) catalog promotion, cache clearing, and user management."""
    role = roles_config.get_role(session.get('role')) or {}
    return render_template(
        'admin.html',
        username=session.get('username'),
        can_manage_users=role.get('can_manage_users', False),
        can_promote_to_catalog=role.get('can_promote_to_catalog', False),
        can_clear_cache=role.get('can_clear_cache', False),
    )


@app.route('/api/queue', methods=['GET'])
@capability_required("can_view_admin_panel")
def get_queue():
    return jsonify(staging_queue.get_queue())


@app.route('/api/promote/<int:entry_id>', methods=['POST'])
@capability_required("can_promote_to_catalog")
def promote_entry(entry_id):
    entry = staging_queue.get_entry(entry_id)
    if entry is None:
        return jsonify({"status": "error", "message": "Entry not found."}), 404
    if entry["status"] != "Approved":
        return jsonify({"status": "error", "message": "Only Approved entries can be promoted."}), 400
    # The catalog is a GLOBAL shortcut with no row-level filter applied
    # to matches (see the Path 3 catalog check below, and
    # catalog_manager.promote). A query that was answered inside one
    # specific donor's session cannot safely become a shared shortcut --
    # its SQL has no WHERE DonorId=... baked in (that's only applied at
    # execution time), so replaying it for a *different* asker later
    # would return unfiltered, cross-donor data. Eligibility is judged
    # by whether the ORIGINATING role was row-restricted (donor), not by
    # whether it happens to be named "admin" -- viewer/analyst/admin are
    # all unrestricted-table roles now, so any of their questions are
    # equally safe to promote.
    if roles_config.is_row_restricted(entry["role_name"]):
        return jsonify({
            "status": "error",
            "message": "Only questions originally asked under an unrestricted role can be promoted "
                        "to the shared catalog -- a donor-scoped answer isn't safe to reuse for everyone."
        }), 400

    catalog_manager.promote(entry["question"], entry["sql"])
    staging_queue.mark_promoted(entry_id)
    return jsonify({"status": "success", "message": "Promoted to catalog."}), 200


@app.route('/api/clear-cache', methods=['POST'])
@capability_required("can_clear_cache")
def clear_cache():
    # Optional ?role=donor to clear just that role's cache; omitted/blank clears everything.
    role_name = request.args.get('role') or None
    query_cache.clear_cache(role_name=role_name)
    return jsonify({"status": "success", "message": "Cache cleared."}), 200


@app.route('/api/conversation/clear', methods=['POST'])
@login_required
def clear_conversation():
    """
    Powers the "New question" control in the UI: explicitly ends the
    current follow-up chain so the next message is always treated as a
    fresh, self-contained question (see conversation_state.py /
    followup_resolver.py) even if it happens to contain a referential
    word like "now". Login-required rather than admin-only -- this is
    per-session and every logged-in user has a conversation to clear.
    """
    conversation_state.clear_state(session.get('conversation_id'))
    return jsonify({"status": "success", "message": "Conversation cleared."}), 200


@app.route('/api/users', methods=['GET'])
@capability_required("can_manage_users")
def list_users():
    return jsonify(auth.list_users())


@app.route('/api/users', methods=['POST'])
@capability_required("can_manage_users")
def create_user_route():
    """
    Provision a new login. Lets an admin create additional donor
    accounts (or additional admins) instead of the app shipping with
    only one hardcoded donor1 login.
    """
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    role_name = data.get('role') or 'donor'
    donor_id = data.get('donor_id')

    if not username or not password:
        return jsonify({"status": "error", "message": "username and password are required."}), 400
    if roles_config.get_role(role_name) is None:
        return jsonify({"status": "error", "message": f"Unknown role '{role_name}'."}), 400
    role = roles_config.get_role(role_name)
    if role["requires_donor_id"]:
        if donor_id is None:
            return jsonify({"status": "error", "message": "donor_id is required for this role."}), 400
        try:
            donor_id = int(donor_id)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "donor_id must be an integer."}), 400
        # Confirm this donor_id actually exists in the role's identity
        # table -- otherwise the account would be scoped to rows that
        # don't exist. identity_table/row_filter_column come from our own
        # roles_config.py (never from request input), so interpolating
        # them as identifiers here is safe -- only the donor_id value
        # itself is user-supplied, and that stays a bound parameter.
        identity_table = role.get("identity_table")
        if identity_table is not None:
            try:
                with engine.connect() as connection:
                    exists = connection.execute(
                        text(f"SELECT 1 FROM {identity_table} WHERE {role['row_filter_column']} = :did"),
                        {"did": donor_id},
                    ).fetchone()
            except SQLAlchemyError as db_error:
                return jsonify({"status": "error", "message": f"Could not verify donor_id: {db_error}"}), 500
            if exists is None:
                return jsonify({
                    "status": "error",
                    "message": f"No {role['label']} with {role['row_filter_column']}={donor_id} exists.",
                }), 400
    else:
        donor_id = None

    try:
        auth.create_user(username, password, role_name, donor_id)
    except sqlite3.IntegrityError:
        return jsonify({"status": "error", "message": f"Username '{username}' already exists."}), 409

    return jsonify({"status": "success", "message": f"User '{username}' created."}), 201


@app.route('/api/analytics/summary', methods=['GET'])
@capability_required("can_view_admin_panel")
def analytics_summary():
    """Aggregate health metrics: hit rates, success rate, avg latency, top failure reasons."""
    days = request.args.get('days')
    return jsonify(query_analytics.get_summary(limit_days=int(days) if days else None)), 200


@app.route('/api/analytics/recent', methods=['GET'])
@capability_required("can_view_admin_panel")
def analytics_recent():
    """Raw recent request log, optionally filtered by ?path=llm|cache|catalog|blocked|error and ?success=0|1."""
    limit = int(request.args.get('limit', 50))
    path = request.args.get('path')
    success_param = request.args.get('success')
    success = None if success_param is None else success_param not in ('0', 'false', 'False')
    return jsonify(query_analytics.get_recent(limit=limit, path=path, success=success)), 200


def _db_connection_error_response(db_error, user_query, role_name, donor_id, timer):
    """
    Shared response shape for a SQLAlchemyError raised while just trying
    to reach the database (bad/missing credentials, DB not reachable,
    wrong DB_NAME, etc.) -- as opposed to a query that reached the
    database and failed there, which gets its own handling in the
    self-healing retry loop below. Factored out because schema-harvest
    failures can now happen at two points in generate_sql (before the
    cache check, and again -- via the same call -- reused for the LLM
    prompt) and both should look identical to the caller.
    """
    error_detail = str(db_error._message()) if hasattr(db_error, '_message') else str(db_error)
    print(f"\n--- DATABASE CONNECTION/QUERY FAILURE ---\n{error_detail}\n----------------------------\n")
    query_analytics.log_query(
        user_query, role_name, donor_id, path="error", success=False, timer=timer,
        failure_reason=f"DB connection error: {error_detail[:280]}",
    )
    return jsonify({
        "status": "error",
        "error_code": "DATABASE_CONNECTION_ERROR",
        "message": "Could not connect to or query the database. Check that .env has correct "
                    "DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME and that the database server "
                    "is running and reachable.",
        "database_error": error_detail
    }), 500


@app.route('/api/generate-sql', methods=['POST'])
@login_required
def generate_sql():
    # 1. Enforce strict JSON data contract
    data = request.get_json()
    if not data or 'query' not in data:
        return jsonify({
            "status": "error",
            "error_code": "INVALID_REQUEST",
            "message": "Missing 'query' parameter in request body."
        }), 400
    
    user_query = data['query']
    timer = query_analytics.QueryTimer()

    # 1b. Resolve role and donor_id from the server-side SESSION, never
    # from the request body. Previously this read data.get('role') and
    # data.get('donor_id') straight from the client's JSON payload,
    # which meant a caller could simply declare itself admin in the
    # request and bypass every access-control check below. Login now
    # happens once at /login; the verified role and donor_id are stored
    # server-side in the session, and every subsequent request is
    # scoped to whatever the session says the caller actually is.
    role_name = session.get('role', 'donor')
    role = roles_config.get_role(role_name)
    if role is None:
        return jsonify({
            "status": "error",
            "error_code": "INVALID_ROLE",
            "message": f"Unknown role '{role_name}'."
        }), 400

    donor_id = session.get('donor_id')
    if role["requires_donor_id"]:
        if donor_id is None:
            return jsonify({
                "status": "error",
                "error_code": "MISSING_DONOR_ID",
                "message": "Your account has no donor_id configured. Contact an administrator."
            }), 400
        try:
            donor_id = int(donor_id)
        except (TypeError, ValueError):
            return jsonify({
                "status": "error",
                "error_code": "INVALID_DONOR_ID",
                "message": "donor_id must be an integer."
            }), 400

    # For an unrestricted role this is just role["allowed_tables"] (None).
    # For a row-filtered role (e.g. donor) it's computed fresh against
    # whatever database is live right now -- see roles_config.py's
    # resolve_allowed_tables docstring for why this isn't a static list.
    allowed_tables = roles_config.resolve_allowed_tables(role_name)
    row_filter_column = role["row_filter_column"]

    # Cache and staging-queue entries are scoped per role+donor via real
    # columns (not a string prefix baked into the question text) so one
    # donor's question -- and answer -- never leaks into another's
    # results, and so a promoted catalog entry can never accidentally
    # carry someone's donor_id along with it. See query_cache.py and
    # staging_queue.py.

    # 2. Input Validation Layer
    if violates_security_matrix(user_query):
        staging_queue.log_entry(user_query, role_name, donor_id, None, "Blocked", "Blocked by input security fence")
        query_analytics.log_query(
            user_query, role_name, donor_id, path="blocked", success=False, timer=timer,
            failure_reason="Blocked by input security fence",
        )
        return jsonify({
            "status": "error",
            "error_code": "SECURITY_VIOLATION",
            "message": "Malicious input signatures detected. Transaction aborted."
        }), 403

    # 2a. Conversation Follow-Up Detection -- must run before catalog/cache,
    # since a follow-up ("sort them by name") is meaningless as a catalog
    # or cache lookup key: those match on question TEXT alone with no
    # notion of "the previous result," so even a coincidental exact-text
    # match would answer the wrong thing. See conversation_state.py and
    # followup_resolver.py for the full design (three tiers: a free
    # in-Python transform of the already-fetched, already-security-filtered
    # previous result; an LLM-assisted SQL modification through the exact
    # same hardened pipeline used below; or -- if this isn't a follow-up
    # at all -- the completely unchanged fresh-question path).
    conversation_id = session.get('conversation_id')
    if not conversation_id:
        conversation_id = secrets.token_hex(16)
        session['conversation_id'] = conversation_id

    conv_state = conversation_state.get_state(conversation_id, role_name, donor_id)
    is_followup = conv_state is not None and followup_resolver.is_followup(user_query, has_active_conversation=True)
    followup_context = None

    if is_followup:
        available_columns = list(conv_state["rows"][0].keys()) if conv_state["rows"] else []
        classification = followup_resolver.classify_operation(user_query, available_columns)

        if classification.operation == "chart":
            # Pure re-visualization of the CURRENT dataset -- no DB or LLM
            # call needed at all.
            new_viz = chart_advisor.recommend(
                conv_state["rows"], forced_type=classification.params.get("forced_type")
            )
            conversation_state.save_state(
                conversation_id, role_name, donor_id, user_query, conv_state["last_sql"],
                conv_state["rows"], new_viz, transform_log=conv_state["transform_log"],
            )
            query_analytics.log_query(
                user_query, role_name, donor_id, path="transform", success=True, timer=timer,
                generated_sql=conv_state["last_sql"], rows_returned=len(conv_state["rows"]),
            )
            return jsonify({
                "status": "success",
                "generated_sql": conv_state["last_sql"],
                "retries_used": 0,
                "data": conv_state["rows"],
                "source": "transform",
                "transform_log": conv_state["transform_log"],
                "visualization": new_viz,
            }), 200

        if classification.operation != "other":
            new_rows = followup_resolver.apply_transform(conv_state["rows"], classification, available_columns)
            if new_rows is not None:
                new_log = conv_state["transform_log"] + [followup_resolver.describe_transform(classification)]
                new_viz = chart_advisor.recommend(new_rows)
                conversation_state.save_state(
                    conversation_id, role_name, donor_id, user_query, conv_state["last_sql"],
                    new_rows, new_viz, transform_log=new_log,
                )
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="transform", success=True, timer=timer,
                    generated_sql=conv_state["last_sql"], rows_returned=len(new_rows),
                )
                return jsonify({
                    "status": "success",
                    "generated_sql": conv_state["last_sql"],
                    "retries_used": 0,
                    "data": new_rows,
                    "source": "transform",
                    "transform_log": new_log,
                    "visualization": new_viz,
                }), 200
            # Recognized the operation but couldn't confidently resolve it
            # (e.g. the sort field didn't match a real column) -- fall
            # through to the LLM-modify tier below rather than guessing.

        # "other", or a recognized-but-unresolved transform: escalate to
        # an LLM-assisted SQL modification. followup_context is threaded
        # into build_system_prompt() below; everything past this point --
        # security fence, table allowlist, row-level filter, self-healing
        # retries -- is the SAME pipeline a fresh question goes through.
        followup_context = {
            "last_question": conv_state["last_question"],
            "last_sql": conv_state["last_sql"],
            "transform_log": conv_state["transform_log"],
        }

    # 2a-bis. Live schema + fingerprint -- harvested once per request,
    # here (rather than down in step 3, where this used to live) so the
    # cache-lookup step below can tell whether the database's shape has
    # changed since an entry was cached, BEFORE trusting that entry.
    # Reused as-is by the LLM prompt-building step further down on a
    # cache/catalog miss, so this is never fetched twice in one request.
    try:
        with timer.phase("schema_harvest"):
            db_dialect, live_schema = extract_live_metadata(allowed_tables=allowed_tables)
    except SQLAlchemyError as db_error:
        return _db_connection_error_response(db_error, user_query, role_name, donor_id, timer)
    schema_fingerprint = compute_schema_fingerprint(live_schema)

    # 2b. Deterministic Catalog Check (Path 3) -- admin-promoted questions
    # skip the LLM entirely and run live against the DB every time, so
    # the data is always fresh even though no model call happens.
    # Restricted to admin: a promoted catalog entry is pre-vetted SQL
    # that bypasses ALL further checks below, so it must not be reachable
    # by a restricted role. Also skipped for follow-ups (see 2a above).
    if not is_followup and role_name == "admin":
        catalog_sql = catalog_manager.find_match(user_query)
        if catalog_sql:
            try:
                with timer.phase("db_exec"), engine.connect() as connection:
                    result = connection.execute(text(catalog_sql))
                    data = [dict(row) for row in result.mappings()]
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="catalog", success=True, timer=timer,
                    generated_sql=catalog_sql, rows_returned=len(data),
                )
                visualization = chart_advisor.recommend(data)
                conversation_state.save_state(
                    conversation_id, role_name, donor_id, user_query, catalog_sql, data,
                    visualization, transform_log=[],
                )
                return jsonify({
                    "status": "success",
                    "generated_sql": catalog_sql,
                    "retries_used": 0,
                    "data": data,
                    "source": "catalog",
                    "visualization": visualization,
                }), 200
            except SQLAlchemyError as db_error:
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="catalog", success=False, timer=timer,
                    generated_sql=catalog_sql, failure_reason=str(db_error)[:300],
                )
                return jsonify({
                    "status": "error",
                    "error_code": "CATALOG_EXECUTION_FAILED",
                    "message": "A promoted catalog query failed to execute.",
                    "database_error": str(db_error)
                }), 500

    # 2c. Cache Lookup -- has this question (or a semantically equivalent
    # rephrasing of it) been asked before by this same role+donor? Exact
    # match is tried first (free, 100% precise); a semantic fallback only
    # runs on an exact-match miss. See query_cache.py for how the two are
    # combined and why that ordering matters for latency. Also skipped
    # for follow-ups (see 2a above).
    #
    # SECURITY: a cache hit must never be a shortcut around access
    # control. Even though this entry can only have been written by
    # THIS SAME role+donor (see query_cache.py's scoping), we still
    # re-check the cached SQL against this role's CURRENT allowed_tables
    # before trusting it -- if roles_config.py has been tightened since
    # this was cached, or anything ever slipped past the write-time
    # check, this stops the stale/bad entry from being replayed forever.
    if not is_followup:
        with timer.phase("cache_lookup"):
            cached = query_cache.get_cached_or_similar(user_query, role_name, donor_id)
        # DATABASE-CHANGE SAFETY: a cached entry's schema_fingerprint is
        # None for rows written before this check existed (trust them,
        # same graceful-degradation convention as the embedding columns)
        # or a real hash otherwise. If it doesn't match the schema we
        # just harvested moments ago, the database has changed shape
        # since this SQL/data was generated -- swapped for a different
        # database, a table renamed, a column dropped, etc. Unlike the
        # disallowed-tables check below, this ISN'T a security violation,
        # so it doesn't error out: it just invalidates the stale entry
        # and falls through to a fresh LLM+DB round trip, exactly as if
        # this had been a cache miss all along.
        if cached and cached["schema_fingerprint"] not in (None, schema_fingerprint):
            print("[Cache] Invalidating entry stale against current schema (fingerprint changed)")
            query_cache.invalidate_by_key(cached["cache_key"])
            cached = None
        if cached:
            table_access_ok, disallowed_tables = access_control.check_table_access(
                cached["generated_sql"], allowed_tables
            )
            if not table_access_ok:
                print(f"[Cache] Invalidating stale entry referencing disallowed tables: {disallowed_tables}")
                query_cache.invalidate_by_key(cached["cache_key"])
                staging_queue.log_entry(
                    user_query, role_name, donor_id, cached["generated_sql"], "Blocked",
                    f"Cached entry invalidated: role '{role_name}' cannot access {disallowed_tables}"
                )
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="blocked", success=False, timer=timer,
                    generated_sql=cached["generated_sql"],
                    failure_reason=f"Cached entry invalidated: role '{role_name}' cannot access {disallowed_tables}",
                )
                return jsonify({
                    "status": "error",
                    "error_code": "ACCESS_DENIED",
                    "message": "This information is not available for your account. Please contact your administrator if you need access."
                }), 403
            query_analytics.log_query(
                user_query, role_name, donor_id, path="cache", success=True, timer=timer,
                generated_sql=cached["generated_sql"], rows_returned=len(cached["data"]),
            )
            visualization = chart_advisor.recommend(cached["data"])
            conversation_state.save_state(
                conversation_id, role_name, donor_id, user_query, cached["generated_sql"],
                cached["data"], visualization, transform_log=[],
            )
            return jsonify({
                "status": "success",
                "generated_sql": cached["generated_sql"],
                "retries_used": 0,
                "data": cached["data"],
                "cached": True,
                "match_type": cached["match_type"],
                "similarity": cached["similarity"],
                "hit_count": cached["hit_count"],
                "visualization": visualization,
            }), 200

    try:
        # 3. Dynamic Context & Dialect Harvesting -- already done above
        # (step 2a-bis), before the cache check. db_dialect/live_schema
        # are reused as-is here, not re-fetched.

        # 4. Strict System Boundary Construction -- see build_system_prompt
        # (shared with the follow-up-modify tier above so the dialect/
        # security/schema rules never drift between the two call sites).
        system_prompt = build_system_prompt(
            db_dialect, live_schema, allowed_tables, donor_id,
            row_filter_column=row_filter_column, followup_context=followup_context,
        )

        # 5. The Execution & Agentic Healing Loop
        max_retries = 2
        attempt = 0
        current_sql = ""
        current_system_prompt = system_prompt
        # Tracks every hallucinated/invalid column name seen across
        # retries so far (case-insensitive), so a column that fails once
        # can be explicitly, permanently forbidden instead of relying on
        # the model to remember and honor a single soft correction.
        forbidden_columns = set()

        while attempt <= max_retries:
            # 5a. Generate SQL. First attempt stays fully deterministic
            # (temperature 0); retries get a bit of randomness so a wrong
            # first guess isn't just regenerated identically forever.
            attempt_temperature = 0.0 if attempt == 0 else 0.4
            with timer.phase("llm"):
                current_sql = call_llm_api(current_system_prompt, user_query, temperature=attempt_temperature)
            
            # ---> THE ENGINEERING FIX: Programmatic Sanitization <---
            # Strip a ```sql fence if present, trailing whitespace, and
            # any trailing semicolon.
            current_sql = strip_markdown_fence(current_sql).rstrip(';')
            
            # Logging
            print(f"\n[Attempt {attempt}] AI Generated: {current_sql}")

            # 5a2. Non-SQL Output Guard -- if the model responded
            # conversationally instead of generating SQL (e.g. the user
            # typed "hi" or something too ambiguous to translate), don't
            # treat this as "broken SQL that needs fixing." Short-circuit
            # immediately instead of entering the self-healing retry loop,
            # which would otherwise pressure the model into hallucinating
            # a syntactically-valid but meaningless query just to comply.
            if not looks_like_sql(current_sql):
                print(f"[Attempt {attempt}] NON-SQL OUTPUT -- short-circuiting, not entering self-healing loop")
                staging_queue.log_entry(
                    user_query, role_name, donor_id, current_sql, "Rejected",
                    "LLM produced non-SQL/conversational output instead of a query"
                )
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="llm", success=False, timer=timer,
                    generated_sql=current_sql, retries_used=attempt,
                    failure_reason="non_sql_llm_output",
                )
                return jsonify({
                    "status": "error",
                    "error_code": "NOT_A_QUERY",
                    "message": "I couldn't turn that into a database query. Try rephrasing it as a "
                                "question about the data, e.g. \"How many active beneficiaries are there?\""
                }), 400

            # 5b. Run Security Fence
            if violates_security_matrix(current_sql):
                print(f"[Attempt {attempt}] BLOCKED BY SECURITY MATRIX") # ---> ADD THIS
                staging_queue.log_entry(user_query, role_name, donor_id, current_sql, "Blocked", "Blocked by output security fence")
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="blocked", success=False, timer=timer,
                    generated_sql=current_sql, retries_used=attempt,
                    failure_reason="Blocked by output security fence",
                )
                return jsonify({
                    "status": "error", 
                    "error_code": "MALICIOUS_OUTPUT_BLOCKED", 
                    "message": "Query violated security matrix."
                }), 403

            # 5b2. Table Access Check -- the generated SQL is only allowed
            # to reference tables this role can see, regardless of what
            # the schema we showed the LLM said. This is the real
            # enforcement boundary, not the schema-hiding above.
            table_access_ok, disallowed_tables = access_control.check_table_access(current_sql, allowed_tables)
            if not table_access_ok:
                print(f"[Attempt {attempt}] ACCESS DENIED -- disallowed tables: {disallowed_tables}")
                staging_queue.log_entry(
                    user_query, role_name, donor_id, current_sql, "Blocked",
                    f"Access denied: role '{role_name}' cannot query {disallowed_tables}"
                )
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="blocked", success=False, timer=timer,
                    generated_sql=current_sql, retries_used=attempt,
                    failure_reason=f"Access denied: role '{role_name}' cannot query {disallowed_tables}",
                )
                return jsonify({
                    "status": "error",
                    "error_code": "ACCESS_DENIED",
                    "message": "This information is not available for your account. Please contact your administrator if you need access."
                }), 403

            # 5b3. Restricted roles may only read, never write -- the
            # row-level filter below only makes sense for SELECTs anyway.
            if allowed_tables is not None and not current_sql.strip().upper().startswith("SELECT"):
                staging_queue.log_entry(user_query, role_name, donor_id, current_sql, "Blocked", "Write operations not permitted for this role")
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="blocked", success=False, timer=timer,
                    generated_sql=current_sql, retries_used=attempt,
                    failure_reason="Write operations not permitted for this role",
                )
                return jsonify({
                    "status": "error",
                    "error_code": "WRITE_NOT_PERMITTED",
                    "message": "Your account is read-only. You can only view your own data, not modify it."
                }), 403

            # 5b4. Row-Level Filter -- force every allowed table reference
            # to be scoped to this donor's own rows, regardless of
            # whether the LLM included a WHERE clause for it.
            execution_sql = access_control.apply_row_level_filter(
                current_sql, allowed_tables, row_filter_column, donor_id
            )

            # 5c. Database Execution Attempt
            try:
                with timer.phase("db_exec"), engine.connect() as connection:
                    result = connection.execute(text(execution_sql))
                    
                    if current_sql.strip().upper().startswith("SELECT"):
                        data = [dict(row) for row in result.mappings()]
                    else:
                        connection.commit()
                        data = None

                if current_sql.strip().upper().startswith("SELECT"):
                    # Only SELECTs are cached: they're read-only and
                    # safe to replay later. Writes (INSERT/UPDATE/etc.)
                    # are never cached -- replaying a stored "success"
                    # message without re-running the write would be
                    # misleading. Follow-up-modified queries are ALSO
                    # never cached under the raw follow-up text ("sort
                    # them by name" style phrasing): that text means
                    # something completely different depending on which
                    # conversation asked it, so caching it globally would
                    # let an unrelated future conversation match a wrong
                    # cached query keyed on the same coincidental words.
                    if not is_followup:
                        query_cache.set_cached(
                            user_query, role_name, donor_id, current_sql, data,
                            schema_fingerprint=schema_fingerprint,
                        )
                    staging_queue.log_entry(user_query, role_name, donor_id, current_sql, "Approved", f"Executed successfully, {attempt} retries")
                    query_analytics.log_query(
                        user_query, role_name, donor_id, path="llm", success=True, timer=timer,
                        generated_sql=current_sql, retries_used=attempt, rows_returned=len(data),
                    )
                    visualization = chart_advisor.recommend(data)
                    # New real SQL just ran -- transform_log resets to []
                    # since any earlier client-side transforms described a
                    # dataset this new query has now replaced outright.
                    conversation_state.save_state(
                        conversation_id, role_name, donor_id, user_query, current_sql, data,
                        visualization, transform_log=[],
                    )
                    return jsonify({
                        "status": "success",
                        "generated_sql": current_sql,
                        "retries_used": attempt,
                        "data": data,
                        "cached": False,
                        "visualization": visualization,
                    }), 200
                else:
                    staging_queue.log_entry(user_query, role_name, donor_id, current_sql, "Approved", f"Executed successfully, {attempt} retries")
                    query_analytics.log_query(
                        user_query, role_name, donor_id, path="llm", success=True, timer=timer,
                        generated_sql=current_sql, retries_used=attempt,
                    )
                    return jsonify({
                        "status": "success", 
                        "generated_sql": current_sql, 
                        "retries_used": attempt, 
                        "message": "Executed successfully."
                    }), 200
            
            # 5d. Catch Database Execution Errors
            except SQLAlchemyError as db_error:
                error_msg = str(db_error._message()) if hasattr(db_error, '_message') else str(db_error)
                print(f"\n--- EXECUTION FAILED (Attempt {attempt + 1}) ---")
                print(f"Failed SQL: {execution_sql}")
                print(f"DB Error: {error_msg}")
                
                # If retry limit hit, fail safely
                if attempt == max_retries:
                    staging_queue.log_entry(user_query, role_name, donor_id, current_sql, "Rejected", error_msg[:300])
                    query_analytics.log_query(
                        user_query, role_name, donor_id, path="llm", success=False, timer=timer,
                        generated_sql=current_sql, retries_used=attempt,
                        failure_reason=error_msg[:300],
                    )
                    return jsonify({
                        "status": "error", 
                        "message": "AI failed to generate a valid query after maximum retry attempts.",
                        "final_sql": current_sql,
                        "database_error": error_msg
                    }), 500
                
                # 5e. Re-frame Context
                print(">>> Triggering AI Self-Healing Prompt...")
                specific_hint, bad_column = build_unknown_column_hint(error_msg, current_sql, live_schema)
                if specific_hint:
                    print(f">>> Specific correction hint: {specific_hint.strip()}")
                if bad_column:
                    forbidden_columns.add(bad_column.lower())

                # A single soft correction isn't always enough -- a weak
                # model can (and does, in practice) ignore it and
                # regenerate the exact same hallucinated column on the
                # very next attempt. Once a column has failed even once,
                # forbid it explicitly and permanently for the rest of
                # this request, rather than relying on the model to
                # remember/honor the earlier hint on its own.
                forbidden_note = ""
                if forbidden_columns:
                    forbidden_list = ", ".join(sorted(forbidden_columns))
                    forbidden_note = (
                        f"\nHARD CONSTRAINT: The following column name(s) have already failed in a "
                        f"previous attempt and do NOT exist anywhere in this schema: {forbidden_list}. "
                        f"You are FORBIDDEN from using them again, under any table alias, in this or "
                        f"any future attempt. If the question doesn't strictly require identifying "
                        f"info like a name, simplest is to omit it entirely.\n"
                    )

                current_system_prompt = f"""You are an expert database administrator. 
Your previous SQL query failed to execute on the '{db_dialect.upper()}' database.

Target Database Schema Context:
{live_schema}

Original User Request: {user_query}
Failed SQL Query: {current_sql}
Database Error Message: {error_msg}
{specific_hint}{forbidden_note}
Analyze the error message. Rewrite the query to fix the syntax, type mismatch, or missing column. 
Return ONLY the corrected raw SQL string. Do not include markdown formatting or explanations."""
                
                attempt += 1

    except SQLAlchemyError as db_error:
        # A DB-connectivity failure (bad/missing credentials, DB not
        # reachable, wrong DB_NAME, etc.) is normally already caught by
        # the schema-harvest try/except above (step 2a-bis) and never
        # reaches here. This is a defensive fallback for any other
        # SQLAlchemyError raised in this block before the retry loop's
        # own handling takes over.
        return _db_connection_error_response(db_error, user_query, role_name, donor_id, timer)

    except Exception as e:
        # 6. FATAL ERROR EXPOSURE
        print(f"\n--- FATAL PIPELINE CRASH ---\n{str(e)}\n----------------------------\n")
        query_analytics.log_query(
            user_query, role_name, donor_id, path="error", success=False, timer=timer,
            failure_reason=f"Fatal pipeline crash: {str(e)[:280]}",
        )
        return jsonify({
            "status": "error",
            "error_code": "INTERNAL_SYSTEM_FAILURE",
            "message": "An unhandled exception occurred in the middleware backend pipeline.",
            "internal_error": str(e)
        }), 500
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
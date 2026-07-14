"""
The actual /api/generate-sql business logic, extracted out of the Flask
route so it can be called, read, and tested as a plain function -- no
Flask request/session context required. The route in routes/query_api.py
is now just: parse the HTTP request, pull role/donor_id/conversation_id
out of the session, call handle_generate_sql(), jsonify the result.

Every function here returns (response_dict, status_code) rather than
calling jsonify() directly, so this module has zero Flask dependency.
"""

import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from db_config import engine
import query_cache
import catalog_manager
import staging_queue
import roles_config
import access_control
import query_analytics
import chart_advisor
import conversation_state
import followup_resolver
from schema_harvester import extract_live_metadata, compute_schema_fingerprint
from llm_client import call_llm_api
from prompt_builder import build_system_prompt
from sql_safety import (
    violates_security_matrix,
    strip_markdown_fence,
    looks_like_sql,
    build_unknown_column_hint,
)

logger = logging.getLogger(__name__)


def _db_connection_error_response(db_error, user_query, role_name, donor_id, timer):
    """
    Shared response shape for a SQLAlchemyError raised while just trying
    to reach the database (bad/missing credentials, DB not reachable,
    wrong DB_NAME, etc.) -- as opposed to a query that reached the
    database and failed there, which gets its own handling in the
    self-healing retry loop below. Factored out because schema-harvest
    failures can happen at two points below and both should look
    identical to the caller.
    """
    error_detail = str(db_error._message()) if hasattr(db_error, '_message') else str(db_error)
    logger.error(
        "Database connection/query failure",
        extra={"role": role_name, "donor_id": donor_id, "db_error": error_detail},
    )
    query_analytics.log_query(
        user_query, role_name, donor_id, path="error", success=False, timer=timer,
        failure_reason=f"DB connection error: {error_detail[:280]}",
    )
    return {
        "status": "error",
        "error_code": "DATABASE_CONNECTION_ERROR",
        "message": "Could not connect to or query the database. Check that .env has correct "
                    "DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME and that the database server "
                    "is running and reachable.",
        "database_error": error_detail
    }, 500


def handle_generate_sql(user_query, role_name, donor_id, conversation_id):
    """
    The full /api/generate-sql pipeline. Returns (response_dict, status_code).

    role_name/donor_id/conversation_id are resolved by the caller from
    the server-side session -- never from the request body. Previously
    this read data.get('role') and data.get('donor_id') straight from
    the client's JSON payload, which meant a caller could simply declare
    itself admin in the request and bypass every access-control check
    below. Login happens once at /login; the verified role and donor_id
    are stored server-side in the session, and every subsequent request
    is scoped to whatever the session says the caller actually is.
    """
    timer = query_analytics.QueryTimer()

    role = roles_config.get_role(role_name)
    if role is None:
        return {
            "status": "error",
            "error_code": "INVALID_ROLE",
            "message": f"Unknown role '{role_name}'."
        }, 400

    if role["requires_donor_id"]:
        if donor_id is None:
            return {
                "status": "error",
                "error_code": "MISSING_DONOR_ID",
                "message": "Your account has no donor_id configured. Contact an administrator."
            }, 400
        try:
            donor_id = int(donor_id)
        except (TypeError, ValueError):
            return {
                "status": "error",
                "error_code": "INVALID_DONOR_ID",
                "message": "donor_id must be an integer."
            }, 400

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
        return {
            "status": "error",
            "error_code": "SECURITY_VIOLATION",
            "message": "Malicious input signatures detected. Transaction aborted."
        }, 403

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
            return {
                "status": "success",
                "generated_sql": conv_state["last_sql"],
                "retries_used": 0,
                "data": conv_state["rows"],
                "source": "transform",
                "transform_log": conv_state["transform_log"],
                "visualization": new_viz,
            }, 200

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
                return {
                    "status": "success",
                    "generated_sql": conv_state["last_sql"],
                    "retries_used": 0,
                    "data": new_rows,
                    "source": "transform",
                    "transform_log": new_log,
                    "visualization": new_viz,
                }, 200
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
    # here, so the cache-lookup step below can tell whether the
    # database's shape has changed since an entry was cached, BEFORE
    # trusting that entry. Reused as-is by the LLM prompt-building step
    # further down on a cache/catalog miss, so this is never fetched
    # twice in one request.
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
                return {
                    "status": "success",
                    "generated_sql": catalog_sql,
                    "retries_used": 0,
                    "data": data,
                    "source": "catalog",
                    "visualization": visualization,
                }, 200
            except SQLAlchemyError as db_error:
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="catalog", success=False, timer=timer,
                    generated_sql=catalog_sql, failure_reason=str(db_error)[:300],
                )
                return {
                    "status": "error",
                    "error_code": "CATALOG_EXECUTION_FAILED",
                    "message": "A promoted catalog query failed to execute.",
                    "database_error": str(db_error)
                }, 500

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
            logger.info(
                "Cache entry invalidated: schema fingerprint changed",
                extra={"role": role_name, "donor_id": donor_id, "cache_key": cached["cache_key"]},
            )
            query_cache.invalidate_by_key(cached["cache_key"])
            cached = None
        if cached:
            table_access_ok, disallowed_tables = access_control.check_table_access(
                cached["generated_sql"], allowed_tables
            )
            if not table_access_ok:
                logger.warning(
                    "Cache entry invalidated: references disallowed tables",
                    extra={"role": role_name, "donor_id": donor_id, "disallowed_tables": sorted(disallowed_tables)},
                )
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
                return {
                    "status": "error",
                    "error_code": "ACCESS_DENIED",
                    "message": "This information is not available for your account. Please contact your administrator if you need access."
                }, 403
            query_analytics.log_query(
                user_query, role_name, donor_id, path="cache", success=True, timer=timer,
                generated_sql=cached["generated_sql"], rows_returned=len(cached["data"]),
            )
            visualization = chart_advisor.recommend(cached["data"])
            conversation_state.save_state(
                conversation_id, role_name, donor_id, user_query, cached["generated_sql"],
                cached["data"], visualization, transform_log=[],
            )
            return {
                "status": "success",
                "generated_sql": cached["generated_sql"],
                "retries_used": 0,
                "data": cached["data"],
                "cached": True,
                "match_type": cached["match_type"],
                "similarity": cached["similarity"],
                "hit_count": cached["hit_count"],
                "visualization": visualization,
            }, 200

    try:
        # 3. Dynamic Context & Dialect Harvesting -- already done above
        # (step 2a-bis). db_dialect/live_schema are reused as-is here,
        # not re-fetched.

        # 4. Strict System Boundary Construction -- see
        # prompt_builder.build_system_prompt (shared with the
        # follow-up-modify tier above so the dialect/security/schema
        # rules never drift between the two call sites).
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

            logger.info(
                "LLM generated SQL",
                extra={"role": role_name, "donor_id": donor_id, "attempt": attempt, "sql": current_sql},
            )

            # 5a2. Non-SQL Output Guard -- if the model responded
            # conversationally instead of generating SQL (e.g. the user
            # typed "hi" or something too ambiguous to translate), don't
            # treat this as "broken SQL that needs fixing." Short-circuit
            # immediately instead of entering the self-healing retry loop,
            # which would otherwise pressure the model into hallucinating
            # a syntactically-valid but meaningless query just to comply.
            if not looks_like_sql(current_sql):
                logger.warning(
                    "LLM produced non-SQL output; short-circuiting instead of entering self-healing loop",
                    extra={"role": role_name, "donor_id": donor_id, "attempt": attempt},
                )
                staging_queue.log_entry(
                    user_query, role_name, donor_id, current_sql, "Rejected",
                    "LLM produced non-SQL/conversational output instead of a query"
                )
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="llm", success=False, timer=timer,
                    generated_sql=current_sql, retries_used=attempt,
                    failure_reason="non_sql_llm_output",
                )
                return {
                    "status": "error",
                    "error_code": "NOT_A_QUERY",
                    "message": "I couldn't turn that into a database query. Try rephrasing it as a "
                                "question about the data, e.g. \"How many active beneficiaries are there?\""
                }, 400

            # 5b. Run Security Fence
            if violates_security_matrix(current_sql):
                logger.warning(
                    "Generated SQL blocked by output security fence",
                    extra={"role": role_name, "donor_id": donor_id, "attempt": attempt, "sql": current_sql},
                )
                staging_queue.log_entry(user_query, role_name, donor_id, current_sql, "Blocked", "Blocked by output security fence")
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="blocked", success=False, timer=timer,
                    generated_sql=current_sql, retries_used=attempt,
                    failure_reason="Blocked by output security fence",
                )
                return {
                    "status": "error",
                    "error_code": "MALICIOUS_OUTPUT_BLOCKED",
                    "message": "Query violated security matrix."
                }, 403

            # 5b2. Table Access Check -- the generated SQL is only allowed
            # to reference tables this role can see, regardless of what
            # the schema we showed the LLM said. This is the real
            # enforcement boundary, not the schema-hiding above.
            table_access_ok, disallowed_tables = access_control.check_table_access(current_sql, allowed_tables)
            if not table_access_ok:
                logger.warning(
                    "Access denied: generated SQL references disallowed tables",
                    extra={"role": role_name, "donor_id": donor_id, "attempt": attempt, "disallowed_tables": sorted(disallowed_tables)},
                )
                staging_queue.log_entry(
                    user_query, role_name, donor_id, current_sql, "Blocked",
                    f"Access denied: role '{role_name}' cannot query {disallowed_tables}"
                )
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="blocked", success=False, timer=timer,
                    generated_sql=current_sql, retries_used=attempt,
                    failure_reason=f"Access denied: role '{role_name}' cannot query {disallowed_tables}",
                )
                return {
                    "status": "error",
                    "error_code": "ACCESS_DENIED",
                    "message": "This information is not available for your account. Please contact your administrator if you need access."
                }, 403

            # 5b3. Restricted roles may only read, never write -- the
            # row-level filter below only makes sense for SELECTs anyway.
            if allowed_tables is not None and not current_sql.strip().upper().startswith("SELECT"):
                staging_queue.log_entry(user_query, role_name, donor_id, current_sql, "Blocked", "Write operations not permitted for this role")
                query_analytics.log_query(
                    user_query, role_name, donor_id, path="blocked", success=False, timer=timer,
                    generated_sql=current_sql, retries_used=attempt,
                    failure_reason="Write operations not permitted for this role",
                )
                return {
                    "status": "error",
                    "error_code": "WRITE_NOT_PERMITTED",
                    "message": "Your account is read-only. You can only view your own data, not modify it."
                }, 403

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
                    return {
                        "status": "success",
                        "generated_sql": current_sql,
                        "retries_used": attempt,
                        "data": data,
                        "cached": False,
                        "visualization": visualization,
                    }, 200
                else:
                    staging_queue.log_entry(user_query, role_name, donor_id, current_sql, "Approved", f"Executed successfully, {attempt} retries")
                    query_analytics.log_query(
                        user_query, role_name, donor_id, path="llm", success=True, timer=timer,
                        generated_sql=current_sql, retries_used=attempt,
                    )
                    return {
                        "status": "success",
                        "generated_sql": current_sql,
                        "retries_used": attempt,
                        "message": "Executed successfully."
                    }, 200

            # 5d. Catch Database Execution Errors
            except SQLAlchemyError as db_error:
                error_msg = str(db_error._message()) if hasattr(db_error, '_message') else str(db_error)
                logger.warning(
                    "SQL execution failed",
                    extra={
                        "role": role_name, "donor_id": donor_id, "attempt": attempt,
                        "sql": execution_sql, "db_error": error_msg,
                    },
                )

                # If retry limit hit, fail safely
                if attempt == max_retries:
                    staging_queue.log_entry(user_query, role_name, donor_id, current_sql, "Rejected", error_msg[:300])
                    query_analytics.log_query(
                        user_query, role_name, donor_id, path="llm", success=False, timer=timer,
                        generated_sql=current_sql, retries_used=attempt,
                        failure_reason=error_msg[:300],
                    )
                    return {
                        "status": "error",
                        "message": "AI failed to generate a valid query after maximum retry attempts.",
                        "final_sql": current_sql,
                        "database_error": error_msg
                    }, 500

                # 5e. Re-frame Context
                logger.info("Triggering AI self-healing retry", extra={"role": role_name, "attempt": attempt})
                specific_hint, bad_column = build_unknown_column_hint(error_msg, current_sql, live_schema)
                if specific_hint:
                    logger.debug("Self-healing correction hint", extra={"hint": specific_hint.strip()})
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
        logger.exception(
            "Fatal pipeline crash",
            extra={"role": role_name, "donor_id": donor_id},
        )
        query_analytics.log_query(
            user_query, role_name, donor_id, path="error", success=False, timer=timer,
            failure_reason=f"Fatal pipeline crash: {str(e)[:280]}",
        )
        return {
            "status": "error",
            "error_code": "INTERNAL_SYSTEM_FAILURE",
            "message": "An unhandled exception occurred in the middleware backend pipeline.",
            "internal_error": str(e)
        }, 500

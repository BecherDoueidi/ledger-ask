"""
Role-based access configuration.

This is the single source of truth for what each role is allowed to see
and do. Two kinds of restriction, both enforced server-side (never trust
the LLM to "behave" -- it only ever sees what we choose to show it, and
everything it outputs is re-checked before it touches the database):

1. allowed_tables / row_filter_column: control DATA access.
   - row_filter_column is None, allowed_tables is None: fully
     unrestricted (viewer/analyst/admin).
   - row_filter_column is set: allowed_tables is NOT read from this
     config at all -- app.py computes it fresh, on every request, via
     schema_harvester.discover_row_scoped_tables(row_filter_column):
     every live table that currently has a column with that name. This
     is what lets the donor role survive a database swap without
     anyone hand-updating a table list here -- rename "Donations" to
     "Contributions" and it's still discovered, as long as it still
     carries a DonorId column. Every discovered table gets an automatic
     "WHERE <row_filter_column> = <donor_id>" wrapped around it before
     execution, so a donor only ever sees rows belonging to them -- even
     if the LLM "forgets" to add that condition itself. (The
     "allowed_tables" key is still present below, set to None, purely
     so get_role()'s return shape is uniform across every role --
     nothing ever reads it directly for a row-filtered role.)

2. can_* capability flags: control APP access -- which admin-side pages
   and actions a logged-in staff member can reach. These were split out
   of a single monolithic "admin" role into three tiers (viewer <
   analyst < admin) so that day-to-day query/reporting access doesn't
   require handing out the same account that can promote catalog
   entries, wipe the shared cache, or create/manage other users.

None of viewer/analyst/admin have a row_filter_column -- they're all
internal staff querying the full database, not self-service donors.
What differs between them is purely which admin-side actions they may
take; the SQL output fence in app.py already blocks DML/DDL for every
role regardless of tier.
"""

ROLES = {
    "viewer": {
        "label": "Viewer (read-only reporting)",
        "allowed_tables": None,       # None = every table in the DB
        "row_filter_column": None,    # None = no row-level restriction
        "requires_donor_id": False,
        # Can ask questions and see results/charts, but has no access to
        # the admin panel, staging queue, analytics, catalog promotion,
        # cache clearing, or user management at all.
        "can_view_admin_panel": False,
        "can_manage_users": False,
        "can_promote_to_catalog": False,
        "can_clear_cache": False,
    },
    "analyst": {
        "label": "Analyst (query + analytics access)",
        "allowed_tables": None,
        "row_filter_column": None,
        "requires_donor_id": False,
        # Can see the admin panel: staging queue and analytics, for
        # visibility into how the system is being used -- but cannot
        # promote entries to the shared catalog, clear the cache, or
        # manage user accounts. Those remain admin-only, since they
        # mutate shared state or affect every other user.
        "can_view_admin_panel": True,
        "can_manage_users": False,
        "can_promote_to_catalog": False,
        "can_clear_cache": False,
    },
    "admin": {
        "label": "Admin (full access)",
        "allowed_tables": None,
        "row_filter_column": None,
        "requires_donor_id": False,
        "can_view_admin_panel": True,
        "can_manage_users": True,
        "can_promote_to_catalog": True,
        "can_clear_cache": True,
    },
    "donor": {
        "label": "Donor (self-service)",
        # Not read directly -- see the module docstring. app.py computes
        # the real table list on every request via
        # schema_harvester.discover_row_scoped_tables("DonorId").
        "allowed_tables": None,
        "row_filter_column": "DonorId",
        "requires_donor_id": True,
        # The table used to validate a donor_id actually exists when an
        # admin creates a new donor account (see app.py's
        # create_user_route). Unlike allowed_tables, this genuinely can't
        # be derived automatically -- nothing in a schema says "this is
        # the canonical identity table" when multiple tables share a
        # DonorId column -- so it's the one piece of donor-specific
        # knowledge that still needs updating by hand if the identity
        # table is renamed.
        "identity_table": "Donors",
        "can_view_admin_panel": False,
        "can_manage_users": False,
        "can_promote_to_catalog": False,
        "can_clear_cache": False,
    },
}


def get_role(role_name):
    """Returns the role config dict, or None if the role doesn't exist."""
    return ROLES.get(role_name)


def has_capability(role_name, capability):
    """
    True only if role_name exists AND that role's config has the given
    can_* flag set. Used to gate admin-side routes/UI -- see
    capability_required in app.py.
    """
    role = ROLES.get(role_name)
    return bool(role and role.get(capability))


def resolve_allowed_tables(role_name):
    """
    The table list app.py should actually enforce for this role, right
    now, against whatever database is currently connected. For an
    unrestricted role this is just the static config value (None). For
    a row-filtered role (row_filter_column set) it's computed fresh via
    schema_harvester.discover_row_scoped_tables -- see the module
    docstring for why. Centralized here (rather than inlined in app.py)
    so every call site gets the same rule and there's exactly one place
    that knows "row-filtered roles don't read allowed_tables from
    config." Returns None (unrestricted) if role_name doesn't exist;
    callers that need to distinguish "no such role" should check
    get_role() first.
    """
    role = ROLES.get(role_name)
    if role is None:
        return None
    if role["row_filter_column"] is None:
        return role["allowed_tables"]
    # Imported here, not at module level, purely to avoid paying for a
    # SQLAlchemy import (schema_harvester -> db_config -> sqlalchemy) in
    # code paths that never touch a row-filtered role.
    import schema_harvester
    return schema_harvester.discover_row_scoped_tables(role["row_filter_column"])


def is_row_restricted(role_name):
    """
    True if this role's data access is scoped to a single row owner
    (currently just "donor"). Used by the catalog-promotion safety check
    in app.py: a question answered under a row-restricted role has no
    WHERE clause baked into its SQL, so it can't safely become a shared
    catalog shortcut for every other user.
    """
    role = ROLES.get(role_name)
    return bool(role and role.get("row_filter_column") is not None)

"""
Role-based access configuration.

This is the single source of truth for what each role is allowed to see
and do. Two kinds of restriction, both enforced server-side (never trust
the LLM to "behave" -- it only ever sees what we choose to show it, and
everything it outputs is re-checked before it touches the database):

1. allowed_tables / row_filter_column: control DATA access, exactly as
   before. None = no restriction (sees/can query every table). If
   row_filter_column is set, every allowed table gets an automatic
   "WHERE <row_filter_column> = <donor_id>" wrapped around it before
   execution, so a donor only ever sees rows belonging to them -- even
   if the LLM "forgets" to add that condition itself.

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
        # Only these tables exist as far as the LLM/SQL is concerned.
        "allowed_tables": ["Donors", "Donations", "Sponsorships", "EventDonations"],
        # Every one of the tables above has a DonorId column linking it
        # back to a specific donor -- that's what we filter on.
        "row_filter_column": "DonorId",
        "requires_donor_id": True,
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

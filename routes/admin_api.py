import sqlite3

from flask import Blueprint, request, jsonify
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from db_config import engine
import staging_queue
import catalog_manager
import query_cache
import roles_config
import auth
from auth_decorators import capability_required

admin_api_bp = Blueprint("admin_api", __name__)


@admin_api_bp.route('/api/queue', methods=['GET'])
@capability_required("can_view_admin_panel")
def get_queue():
    return jsonify(staging_queue.get_queue())


@admin_api_bp.route('/api/promote/<int:entry_id>', methods=['POST'])
@capability_required("can_promote_to_catalog")
def promote_entry(entry_id):
    entry = staging_queue.get_entry(entry_id)
    if entry is None:
        return jsonify({"status": "error", "message": "Entry not found."}), 404
    if entry["status"] != "Approved":
        return jsonify({"status": "error", "message": "Only Approved entries can be promoted."}), 400
    # The catalog is a GLOBAL shortcut with no row-level filter applied
    # to matches (see the Path 3 catalog check in query_service.py, and
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


@admin_api_bp.route('/api/clear-cache', methods=['POST'])
@capability_required("can_clear_cache")
def clear_cache():
    # Optional ?role=donor to clear just that role's cache; omitted/blank clears everything.
    role_name = request.args.get('role') or None
    query_cache.clear_cache(role_name=role_name)
    return jsonify({"status": "success", "message": "Cache cleared."}), 200


@admin_api_bp.route('/api/users', methods=['GET'])
@capability_required("can_manage_users")
def list_users():
    return jsonify(auth.list_users())


@admin_api_bp.route('/api/users', methods=['POST'])
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

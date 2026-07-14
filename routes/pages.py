from flask import Blueprint, render_template, session

import roles_config
from auth_decorators import login_required, capability_required

pages_bp = Blueprint("pages", __name__)


@pages_bp.route('/')
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


@pages_bp.route('/admin')
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

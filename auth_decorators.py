"""
Route-protecting decorators shared across every blueprint in routes/.
Pulled out of app.py so the auth/capability-checking logic is defined
once, independent of which specific routes use it.
"""

from functools import wraps
from flask import request, jsonify, redirect, url_for, session

import roles_config


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
            return redirect(url_for("auth.login"))
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
                return redirect(url_for("auth.login"))
            if not roles_config.has_capability(session.get("role"), capability):
                if request.path.startswith("/api/"):
                    return jsonify({
                        "status": "error",
                        "error_code": "ACCESS_DENIED",
                        "message": "You don't have permission to do this."
                    }), 403
                return redirect(url_for("pages.index"))
            return view(*args, **kwargs)
        return wrapped
    return decorator

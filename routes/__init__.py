"""
Flask Blueprints grouped by concern:
  - auth_routes:      /login, /logout
  - pages:             /, /admin (server-rendered HTML)
  - admin_api:         staging queue, catalog promotion, cache, user management
  - analytics_api:     read-only observability endpoints
  - query_api:         /api/generate-sql, /api/conversation/clear

register_blueprints(app) is the one thing app.py needs to call.
"""

from .auth_routes import auth_bp
from .pages import pages_bp
from .admin_api import admin_api_bp
from .analytics_api import analytics_api_bp
from .query_api import query_api_bp


def register_blueprints(app):
    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(admin_api_bp)
    app.register_blueprint(analytics_api_bp)
    app.register_blueprint(query_api_bp)

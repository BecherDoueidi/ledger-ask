from flask import Blueprint, request, jsonify

import query_analytics
from auth_decorators import capability_required

analytics_api_bp = Blueprint("analytics_api", __name__)


@analytics_api_bp.route('/api/analytics/summary', methods=['GET'])
@capability_required("can_view_admin_panel")
def analytics_summary():
    """Aggregate health metrics: hit rates, success rate, avg latency, top failure reasons."""
    days = request.args.get('days')
    return jsonify(query_analytics.get_summary(limit_days=int(days) if days else None)), 200


@analytics_api_bp.route('/api/analytics/recent', methods=['GET'])
@capability_required("can_view_admin_panel")
def analytics_recent():
    """Raw recent request log, optionally filtered by ?path=llm|cache|catalog|blocked|error and ?success=0|1."""
    limit = int(request.args.get('limit', 50))
    path = request.args.get('path')
    success_param = request.args.get('success')
    success = None if success_param is None else success_param not in ('0', 'false', 'False')
    return jsonify(query_analytics.get_recent(limit=limit, path=path, success=success)), 200

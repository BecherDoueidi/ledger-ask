import secrets

from flask import Blueprint, request, jsonify, session

import conversation_state
import query_service
from auth_decorators import login_required

query_api_bp = Blueprint("query_api", __name__)


@query_api_bp.route('/api/conversation/clear', methods=['POST'])
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


@query_api_bp.route('/api/generate-sql', methods=['POST'])
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

    # Role and donor_id come from the server-side SESSION, never from
    # the request body -- see query_service.handle_generate_sql's
    # docstring for why.
    role_name = session.get('role', 'donor')
    donor_id = session.get('donor_id')

    conversation_id = session.get('conversation_id')
    if not conversation_id:
        conversation_id = secrets.token_hex(16)
        session['conversation_id'] = conversation_id

    response_body, status_code = query_service.handle_generate_sql(
        user_query, role_name, donor_id, conversation_id
    )
    return jsonify(response_body), status_code

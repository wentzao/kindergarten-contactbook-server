"""
Admin authentication decorator.

Endpoints decorated with @require_admin require an X-Admin-Token header that
matches the ADMIN_TOKEN environment variable. If ADMIN_TOKEN is not set, the
endpoint refuses all calls (fail-closed) — we never want admin endpoints to be
publicly callable due to misconfiguration.

Set the token in the gunicorn .env or via systemd EnvironmentFile:
    ADMIN_TOKEN=<long-random-secret>
"""
import os
from functools import wraps
from flask import request, jsonify

ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '').strip()


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not ADMIN_TOKEN:
            return jsonify({
                'error': 'Admin endpoint disabled: ADMIN_TOKEN env var not configured.'
            }), 503
        provided = request.headers.get('X-Admin-Token', '').strip()
        if not provided or provided != ADMIN_TOKEN:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

"""REST endpoints for class notification grants.

A grant is the authoritative "parent can view (class, date)" record.
Old endpoints (/api/class-journal/<class>/<date>/publish,
/api/contact-book/<sid>/<date>/publish, /api/notifications/send-batch) all
delegate to this service now.
"""
import os
from flask import Blueprint, jsonify, request

from services.data_service import DataService
from services import notification_grant_service as grant_service


grants_bp = Blueprint('notification_grants', __name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)


def _bad_request(message):
    return jsonify({'error': message}), 400


# ──────────────────────────────────────────────────────────
# Grant (immediate or dismissal-time)
# ──────────────────────────────────────────────────────────

@grants_bp.route('/<class_name>/<date>', methods=['POST'])
def create_grant(class_name, date):
    """Grant parent-visibility for one (class, date).

    Body:
      {
        "studentIds": ["sid1","sid2", ...],   # required, explicit list
        "studentNames": { "sid1": "...", ... }, # optional, for push body
        "sentBy": "userId",                   # optional
        "mode": "immediate" | "dismissal"     # default: immediate
      }
    """
    data = request.get_json(silent=True) or {}
    student_ids = data.get('studentIds') or []
    if not isinstance(student_ids, list) or not student_ids:
        return _bad_request('studentIds (non-empty array) is required')

    mode = (data.get('mode') or 'immediate').strip().lower()
    sent_by = data.get('sentBy') or ''
    student_names = data.get('studentNames') or {}

    try:
        if mode == 'dismissal':
            result = grant_service.schedule_dismissal_grant(
                data_service, class_name, date, student_ids,
                sent_by=sent_by, student_names=student_names,
            )
        elif mode == 'immediate':
            result = grant_service.grant_now(
                data_service, class_name, date, student_ids,
                sent_by=sent_by, student_names=student_names,
            )
        else:
            return _bad_request("mode must be 'immediate' or 'dismissal'")
        return jsonify(result), 200
    except ValueError as e:
        return _bad_request(str(e))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@grants_bp.route('/<class_name>/<date>', methods=['DELETE'])
def cancel_grant(class_name, date):
    """Cancel a grant (soft-delete) — parents stop seeing this (class, date).

    Body (optional): { "cancelledBy": "userId" }
    """
    data = request.get_json(silent=True) or {}
    cancelled_by = data.get('cancelledBy') or ''
    try:
        result = grant_service.cancel_grant(
            data_service, class_name, date, cancelled_by=cancelled_by,
        )
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@grants_bp.route('/<class_name>/<date>/schedule', methods=['DELETE'])
def cancel_scheduled_grant(class_name, date):
    """Cancel a pending dismissal-time scheduled grant.

    Body (optional): { "cancelledBy": "userId" }
    """
    data = request.get_json(silent=True) or {}
    cancelled_by = data.get('cancelledBy') or ''
    try:
        result = grant_service.cancel_scheduled_grant(
            data_service, class_name, date, cancelled_by=cancelled_by,
        )
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ──────────────────────────────────────────────────────────
# Reads
# ──────────────────────────────────────────────────────────

@grants_bp.route('/<class_name>/<date>', methods=['GET'])
def get_grant_route(class_name, date):
    """Return the grant for (class, date), or null."""
    conn = data_service.get_db()
    try:
        row = grant_service.get_grant(conn, class_name, date)
        return jsonify(grant_service.serialize_grant(row)), 200
    finally:
        conn.close()


@grants_bp.route('/<class_name>/<date>/events', methods=['GET'])
def get_events_route(class_name, date):
    """Diagnostic: recent grant/cancel/schedule events for this (class, date)."""
    limit = min(max(request.args.get('limit', 20, type=int), 1), 100)
    conn = data_service.get_db()
    try:
        events = grant_service.get_events(conn, class_name, date, limit=limit)
        return jsonify({'events': events}), 200
    finally:
        conn.close()


@grants_bp.route('/student/<student_id>', methods=['GET'])
def get_visible_dates_for_student_route(student_id):
    """Parent-facing: list dates the student can view.

    Query params:
      className=<str>   (optional, filter)
      yearMonth=<YYYY-MM> (optional, filter)
    """
    class_name = request.args.get('className')
    year_month = request.args.get('yearMonth')
    date_like = f'{year_month}-%' if year_month else None
    conn = data_service.get_db()
    try:
        rows = grant_service.get_visible_grants_for_student(
            conn, student_id, class_name=class_name, date_like=date_like,
        )
        return jsonify({
            'studentId': student_id,
            'grants': [grant_service.serialize_grant(r) for r in rows],
        }), 200
    finally:
        conn.close()

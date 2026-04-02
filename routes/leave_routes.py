from flask import Blueprint, request, jsonify
from services.data_service import DataService
from datetime import datetime, date
import os

leave_bp = Blueprint('leave', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)


@leave_bp.route('/today', methods=['GET'])
def get_today_leaves():
    """Get all leave records effective today (across all students)."""
    today_str = date.today().isoformat()
    conn = data_service.get_db()
    try:
        rows = conn.execute('''
            SELECT * FROM leave_records
            WHERE start_date <= ? AND end_date >= ?
            ORDER BY child_id
        ''', (today_str, today_str)).fetchall()
        records = [{
            'id': r['id'], 'childId': r['child_id'], 'type': r['type'],
            'startDate': r['start_date'], 'endDate': r['end_date'],
            'reason': r['reason'], 'createdBy': r['created_by'],
            'createdAt': r['created_at'],
        } for r in rows]
        return jsonify(records)
    finally:
        conn.close()


@leave_bp.route('/month', methods=['GET'])
def get_month_leaves():
    """Get all leave records for a month. Query param: month=YYYY-MM"""
    month = request.args.get('month', date.today().strftime('%Y-%m'))
    year, mon = month.split('-')
    start = f"{year}-{mon}-01"
    import calendar
    last_day = calendar.monthrange(int(year), int(mon))[1]
    end = f"{year}-{mon}-{last_day:02d}"
    conn = data_service.get_db()
    try:
        rows = conn.execute('''
            SELECT * FROM leave_records
            WHERE start_date <= ? AND end_date >= ?
            ORDER BY start_date
        ''', (end, start)).fetchall()
        records = [{
            'id': r['id'], 'childId': r['child_id'], 'type': r['type'],
            'startDate': r['start_date'], 'endDate': r['end_date'],
            'reason': r['reason'], 'createdBy': r['created_by'],
            'createdAt': r['created_at'],
        } for r in rows]
        return jsonify(records)
    finally:
        conn.close()

@leave_bp.route('/<child_id>', methods=['GET'])
def get_leave_requests(child_id):
    month = request.args.get('month')
    records = data_service.get_student_records(child_id, 'leave', month)
    records.sort(key=lambda x: x.get('createdAt', ''), reverse=True)
    return jsonify(records)

@leave_bp.route('', methods=['POST'])
@leave_bp.route('/', methods=['POST'])
def submit_leave_request():
    try:
        data = request.json
        if not data or 'childId' not in data:
            return jsonify({'error': 'Missing childId'}), 400
        saved_record = data_service.save_student_record(data['childId'], 'leave', data)
        return jsonify(saved_record), 201
    except Exception as e:
        print(f"[Leave] POST error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@leave_bp.route('/<leave_id>', methods=['DELETE'])
def delete_leave_request(leave_id):
    try:
        success = data_service.delete_student_record(leave_id, 'leave')
        if success:
            return jsonify({'status': 'deleted'}), 200
        else:
            return jsonify({'error': 'Record not found'}), 404
    except Exception as e:
        print(f"[Leave] DELETE error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


from flask import Blueprint, request, jsonify
from services.data_service import DataService
from services.student_request_notification_service import enqueue_student_request_notification
import os

med_bp = Blueprint('meds', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

@med_bp.route('/<child_id>', methods=['GET'])
def get_med_requests(child_id):
    month = request.args.get('month')
    records = data_service.get_student_records(child_id, 'meds', month)
    records.sort(key=lambda x: x.get('createdAt', ''), reverse=True)
    return jsonify(records)

@med_bp.route('/', methods=['POST'])
def submit_med_request():
    data = request.json
    if not data or 'childId' not in data:
        return jsonify({'error': 'Missing childId'}), 400

    saved_record = data_service.save_student_record(data['childId'], 'meds', data)
    try:
        enqueue_student_request_notification(data_service, 'med', saved_record)
    except Exception as notify_error:
        print(f"[Meds] teacher notification enqueue error: {notify_error}")
    return jsonify(saved_record), 201


@med_bp.route('/<med_id>', methods=['DELETE'])
def delete_med_request(med_id):
    try:
        success = data_service.delete_student_record(med_id, 'meds')
        if success:
            return jsonify({'status': 'deleted'}), 200
        else:
            return jsonify({'error': 'Record not found'}), 404
    except Exception as e:
        print(f"[Meds] DELETE error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

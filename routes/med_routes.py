from flask import Blueprint, request, jsonify
from services.data_service import DataService
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
    return jsonify(saved_record), 201

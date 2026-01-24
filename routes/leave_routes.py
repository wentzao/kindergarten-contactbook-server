from flask import Blueprint, request, jsonify
from services.data_service import DataService
import os

leave_bp = Blueprint('leave', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

@leave_bp.route('/<child_id>', methods=['GET'])
def get_leave_requests(child_id):
    month = request.args.get('month')
    print(f"[LEAVE GET] Received request for child_id={child_id}, month={month}")
    records = data_service.get_student_records(child_id, 'leave', month)
    # Sort by createdAt desc
    records.sort(key=lambda x: x.get('createdAt', ''), reverse=True)
    print(f"[LEAVE GET] Returning {len(records)} records")
    return jsonify(records)

@leave_bp.route('', methods=['POST'])
@leave_bp.route('/', methods=['POST'])
def submit_leave_request():
    try:
        print(f"[LEAVE POST] Received POST request")
        print(f"[LEAVE POST] Headers: {dict(request.headers)}")
        print(f"[LEAVE POST] Content-Type: {request.content_type}")
        print(f"[LEAVE POST] Raw data: {request.get_data(as_text=True)}")
        
        data = request.json
        print(f"[LEAVE POST] Parsed JSON data: {data}")
        
        if not data or 'childId' not in data:
            print(f"[LEAVE POST] ERROR: Missing childId")
            return jsonify({'error': 'Missing childId'}), 400
        
        print(f"[LEAVE POST] Saving record for child_id={data['childId']}")
        saved_record = data_service.save_student_record(data['childId'], 'leave', data)
        print(f"[LEAVE POST] Successfully saved record: {saved_record}")
        return jsonify(saved_record), 201
    except Exception as e:
        print(f"[LEAVE POST] EXCEPTION: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@leave_bp.route('/<leave_id>', methods=['DELETE'])
def delete_leave_request(leave_id):
    try:
        print(f"[LEAVE DELETE] Received request for leave_id={leave_id}")
        success = data_service.delete_student_record(leave_id, 'leave')
        if success:
            print(f"[LEAVE DELETE] Successfully deleted record")
            return jsonify({'status': 'deleted'}), 200
        else:
            print(f"[LEAVE DELETE] Record not found")
            return jsonify({'error': 'Record not found'}), 404
    except Exception as e:
        print(f"[LEAVE DELETE] EXCEPTION: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


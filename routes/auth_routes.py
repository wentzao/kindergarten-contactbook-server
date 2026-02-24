from flask import Blueprint, request, jsonify
import os
import json
from datetime import datetime

auth_bp = Blueprint('auth_bp', __name__)

TEACHER_DATA_PATH = r'\\192.168.50.53\server\桌面\punchcard-2022-checker\static\teacher_info\data\teacher_data.json'

@auth_bp.route('/teacher_login', methods=['POST'])
def teacher_login():
    data = request.json
    if not data or 'userId' not in data:
        return jsonify({'error': 'Missing userId'}), 400
    
    user_id = data.get('userId')
    
    try:
        with open(TEACHER_DATA_PATH, 'r', encoding='utf-8') as f:
            teacher_data = json.load(f)
    except Exception as e:
        return jsonify({'error': f'Failed to load teacher data: {str(e)}'}), 500
    
    teachers = teacher_data.get('teachers', [])
    for t in teachers:
        if t.get('userId') == user_id:
            # Found the teacher
            if t.get('status') != '在職':
                return jsonify({'error': 'Teacher is not active'}), 403
            
            category = t.get('category', '')
            is_admin = category in ['管理員', '行政人員']
            
            # 取得授權班級 (根據學期)
            teaching_classes = t.get('teachingClasses', {})
            
            return jsonify({
                'uuid': t.get('uuid'),
                'name': t.get('cname') or t.get('ename'),
                'cname': t.get('cname'),
                'ename': t.get('ename'),
                'category': category,
                'isAdmin': is_admin,
                'teachingClasses': teaching_classes
            }), 200
            
    return jsonify({'error': 'Teacher not found'}), 404

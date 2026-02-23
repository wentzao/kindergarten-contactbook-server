from flask import Blueprint, request, jsonify
import requests
from datetime import datetime

student_bp = Blueprint('student_bp', __name__)

def get_current_semester():
    now = datetime.now()
    if now.month >= 8:
        return f"{now.year - 1911}第1學期"
    elif now.month == 1:
        return f"{now.year - 1912}第1學期"
    else:
        return f"{now.year - 1912}第2學期"

import json

TEACHER_DATA_PATH = r'\\192.168.50.53\server\桌面\punchcard-2022-checker\static\teacher_info\data\teacher_data.json'

@student_bp.route('/classes', methods=['GET'])
def get_classes():
    semester = request.args.get('semester', get_current_semester())
    user_id = request.args.get('userId')
    
    if not user_id:
        return jsonify({'error': 'Missing userId'}), 400
        
    # Validation Teacher Role
    try:
        with open(TEACHER_DATA_PATH, 'r', encoding='utf-8') as f:
            teacher_data = json.load(f)
    except Exception as e:
        return jsonify({'error': f'Failed to load teacher config: {str(e)}'}), 500
        
    teacher = next((t for t in teacher_data.get('teachers', []) if t.get('userId') == user_id), None)
    if not teacher or teacher.get('status') != '在職':
        return jsonify({'error': 'Unauthorized'}), 401
        
    is_admin = teacher.get('category') in ['管理員', '行政人員']
    teaching_classes = [c.get('className') for c in teacher.get('teachingClasses', {}).get(semester, [])]
    
    url = "https://web.wentzao.com/api/get_class_student_info"
    headers = {"Content-Type": "application/json"}
    data = {"semester": semester, "data_format": "flat"}
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        res_json = response.json()
        classes_data = res_json.get('classes', [])
        students_data = res_json.get('students', [])
        
        # classes_data is presumably a list of class dicts or names. 
        # For 'flat', students_data is a list of [id, name, en_name, className, govClass, ...]
        filtered_classes = []
        filtered_students = []
        
        # web.wentzao 'flat' API returns students as lists: [uid, id, cname, ename, class_name, status, etc]
        # In Kindergarten-Official-Account-Line app.py approx Line 258:
        # kid_info[3] is class_name.
        
        for student in students_data:
            # student array map based on the output observed:
            # ['陳科閣', 'Carter', '在校生', 'Sun 1', '太陽一班-大班', 'B125728127', '108.09.03', '2019/09/03', ...]
            # 0: cname, 1: ename, 2: status, 3: class_name, 4: gov_class, 5: student_id
            if len(student) >= 6:
                cname = student[0]
                ename = student[1]
                status = student[2]
                class_name = student[3]
                gov_class = student[4]
                student_id = student[5]
                photo_url = student[10] if len(student) > 10 and 'http' in student[10] else "" 
                uid = student_id # using student_id as uid
            else:
                continue
                
            if is_admin or class_name in teaching_classes:
                student_obj = {
                    "uid": uid,
                    "studentId": student_id,
                    "chineseName": cname,
                    "englishName": ename,
                    "status": status,
                    "className": class_name,
                    "govClass": gov_class,
                    "photoUrl": photo_url
                }
                filtered_students.append(student_obj)
                if class_name not in filtered_classes:
                    filtered_classes.append(class_name)
        
        return jsonify({
            'semester': semester,
            'classes': filtered_classes,
            'students': filtered_students,
            'isAdmin': is_admin
        }), 200
        
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch data from wentzao API: {str(e)}'}), 500

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

WEB_WENTZAO_TEACHER_AUTH_API = 'https://web.wentzao.com/api/get_teacher_for_auth'

@student_bp.route('/classes', methods=['GET'])
def get_classes():
    semester = request.args.get('semester', get_current_semester())
    user_id = request.args.get('userId')
    
    if not user_id:
        return jsonify({'error': 'Missing userId'}), 400
        
    # 透過 web.wentzao.com API 驗證教師身分
    try:
        auth_resp = requests.post(
            WEB_WENTZAO_TEACHER_AUTH_API,
            json={'userId': user_id},
            timeout=10
        )
        if auth_resp.status_code != 200:
            err_msg = auth_resp.json().get('error', 'Unauthorized') if auth_resp.headers.get('content-type', '').startswith('application/json') else 'Unauthorized'
            return jsonify({'error': err_msg}), auth_resp.status_code
        
        teacher = auth_resp.json()
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to verify teacher: {str(e)}'}), 502
        
    is_admin = teacher.get('isAdmin', False)
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
                # student[10] is GoogleDrive folder link, extract folder_id
                drive_link = student[10] if len(student) > 10 else ""
                folder_id = ""
                if drive_link and 'drive.google.com' in drive_link:
                    folder_id = drive_link.split('/')[-1].replace('?usp=drive_link', '')
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
                    "folderId": folder_id
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

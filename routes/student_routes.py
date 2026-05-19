from flask import Blueprint, request, jsonify
import requests
import sqlite3
import os
from datetime import datetime

student_bp = Blueprint('student_bp', __name__)

# Student name cache DB path
_DB_PATH = (
    os.environ.get('KINDERGARTEN_DB_PATH')
    or os.environ.get('DB_PATH')
    or os.path.join(os.path.dirname(os.path.dirname(__file__)), 'kindergarten.db')
)
_DB_TIMEOUT = float(os.environ.get('SQLITE_BUSY_TIMEOUT_SECONDS', '5'))

def _connect_db():
    conn = sqlite3.connect(_DB_PATH, timeout=_DB_TIMEOUT)
    conn.execute(f'PRAGMA busy_timeout={int(_DB_TIMEOUT * 1000)}')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn

def _cache_student_names(students):
    """Cache student names in the database for notification lookups."""
    try:
        conn = _connect_db()
        conn.execute('''CREATE TABLE IF NOT EXISTS student_names (
            student_id VARCHAR(50) PRIMARY KEY,
            chinese_name VARCHAR(100),
            english_name VARCHAR(100)
        )''')
        for s in students:
            conn.execute(
                'INSERT OR REPLACE INTO student_names (student_id, chinese_name, english_name) VALUES (?, ?, ?)',
                (s['studentId'], s.get('chineseName', ''), s.get('englishName', ''))
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[StudentNames] Cache error: {e}')


def _cache_teacher_scope(user_id, semester, teacher, teaching_classes, students):
    """Cache teacher → class and student → class mapping for targeted notifications."""
    try:
        conn = _connect_db()
        now = datetime.now().isoformat()
        conn.execute('''CREATE TABLE IF NOT EXISTS teacher_class_memberships (
            user_id VARCHAR(100) NOT NULL,
            semester VARCHAR(50) NOT NULL,
            class_name VARCHAR(100) NOT NULL,
            is_admin BOOLEAN DEFAULT 0,
            updated_at VARCHAR(50),
            PRIMARY KEY (user_id, semester, class_name)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS student_class_cache (
            student_id VARCHAR(50) NOT NULL,
            semester VARCHAR(50) NOT NULL,
            class_name VARCHAR(100) NOT NULL,
            chinese_name VARCHAR(100),
            english_name VARCHAR(100),
            updated_at VARCHAR(50),
            PRIMARY KEY (student_id, semester)
        )''')
        conn.execute(
            'DELETE FROM teacher_class_memberships WHERE user_id = ? AND semester = ?',
            (user_id, semester)
        )
        normalized_classes = []
        seen = set()
        for class_name in teaching_classes:
            class_name = (class_name or '').strip()
            if class_name and class_name not in seen:
                seen.add(class_name)
                normalized_classes.append(class_name)
        for class_name in normalized_classes:
            conn.execute('''
                INSERT OR REPLACE INTO teacher_class_memberships
                    (user_id, semester, class_name, is_admin, updated_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, semester, class_name, 1 if teacher.get('isAdmin', False) else 0, now))

        for student in students:
            conn.execute('''
                INSERT OR REPLACE INTO student_class_cache
                    (student_id, semester, class_name, chinese_name, english_name, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                student.get('studentId', ''),
                semester,
                student.get('className', ''),
                student.get('chineseName', ''),
                student.get('englishName', ''),
                now,
            ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[TeacherScope] Cache error: {e}')

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

def _is_truthy(value):
    return str(value).strip().lower() in ('1', 'true', 'yes', 'y', 'on')

@student_bp.route('/classes', methods=['GET'])
def get_classes():
    semester = request.args.get('semester', get_current_semester())
    user_id = request.args.get('userId')
    show_all_data_raw = request.args.get('showAllData')
    has_explicit_data_scope = show_all_data_raw is not None
    show_all_data = _is_truthy(show_all_data_raw)
    
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
    # Keep legacy web behavior when no scope is provided, but let mobile clients
    # explicitly request managed-class mode for admin test accounts.
    include_all_classes = is_admin and (
        show_all_data if has_explicit_data_scope else True
    )
    teaching_classes = [
        c.get('className')
        for c in teacher.get('teachingClasses', {}).get(semester, [])
        if c.get('className')
    ]
    
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
                # GoogleDrive field: extract folder_id from drive link (index 9 = last element)
                drive_link = student[9] if len(student) > 9 else ""
                folder_id = ""
                if drive_link and 'drive.google.com' in drive_link:
                    folder_id = drive_link.split('/')[-1].replace('?usp=drive_link', '').split('?')[0]
                uid = student_id # using student_id as uid
            else:
                continue
                
            # isAdmin only marks permission. showAllData must explicitly opt in
            # so admins can still test the same managed-class scope as teachers.
            if include_all_classes or class_name in teaching_classes:
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
        
        # Cache student names for notification lookups
        if filtered_students:
            _cache_student_names(filtered_students)
        _cache_teacher_scope(
            user_id,
            semester,
            teacher,
            teaching_classes if teaching_classes else filtered_classes,
            filtered_students
        )

        return jsonify({
            'semester': semester,
            'classes': filtered_classes,
            'students': filtered_students,
            'isAdmin': is_admin,
            'dataScope': 'allClasses' if include_all_classes else 'managedClasses'
        }), 200
        
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch data from wentzao API: {str(e)}'}), 500


# Proxy endpoint: fetch student photos from student.wentzao.com
@student_bp.route('/student_photos', methods=['POST'])
def get_student_photos():
    try:
        data = request.get_json()
        folder_ids = data.get('folder_ids', [])
        
        if not folder_ids:
            return jsonify({'error': 'No folder_ids provided'}), 400
        
        response = requests.post(
            'https://student.wentzao.com/get_photo_data',
            json={'folder_ids': folder_ids},
            timeout=15
        )
        return jsonify(response.json()), response.status_code
        
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch photos: {str(e)}'}), 500


# Proxy endpoint: get Google Drive API key for video playback
# Uses POST to match existing CORS-safe pattern (same as student_photos)
@student_bp.route('/video_api_key', methods=['POST', 'OPTIONS'])
def get_video_api_key():
    if request.method == 'OPTIONS':
        resp = jsonify({'status': 'ok'})
        resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return resp, 200

    try:
        r = requests.get('https://student.wentzao.com/get_api_key', timeout=5)
        if r.status_code != 200:
            return jsonify({'error': 'Failed to get API key'}), 502
        return jsonify(r.json()), 200
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'API key fetch error: {str(e)}'}), 500

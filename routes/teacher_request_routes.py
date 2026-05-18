from collections import OrderedDict
from datetime import date
import json
import os

from flask import Blueprint, jsonify, request

from services.data_service import DataService


teacher_requests_bp = Blueprint('teacher_requests', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

MED_REASON_LABELS = {
    'cold': '感冒',
    'gastro': '腸胃炎',
    'allergy': '過敏',
    'other': '其他',
}

MED_TIME_LABELS = {
    'breakfast_before': '早餐前',
    'breakfast_after': '早餐後',
    'snack_before': '點心前',
    'snack_after': '點心後',
    'lunch_before': '午餐前',
    'lunch_after': '午餐後',
    'dinner_before': '晚餐前',
    'dinner_after': '晚餐後',
    'bedtime': '睡前',
}


@teacher_requests_bp.route('/summary', methods=['GET'])
def get_teacher_request_summary():
    """Return leave and medication requests for one class on one day."""
    class_name = (request.args.get('className') or '').strip()
    target_date = (request.args.get('date') or date.today().isoformat()).strip()
    semester = (request.args.get('semester') or '').strip()

    if not class_name:
        return jsonify({'error': 'className query param required'}), 400
    if not target_date:
        return jsonify({'error': 'date query param required'}), 400

    conn = data_service.get_db()
    try:
        class_students = _load_class_students(conn, class_name, semester)
        leave_rows = _load_leave_rows(conn, class_name, target_date, semester)
        med_rows = _load_med_rows(conn, class_name, target_date, semester)

        leave_students = _group_leave_students(leave_rows)
        med_students = _group_med_students(med_rows)

        return jsonify({
            'className': class_name,
            'date': target_date,
            'semester': semester or None,
            'totalStudents': len(class_students),
            'leave': {
                'studentCount': len(leave_students),
                'recordCount': len(leave_rows),
                'students': list(leave_students.values()),
            },
            'meds': {
                'studentCount': len(med_students),
                'recordCount': len(med_rows),
                'students': list(med_students.values()),
            },
            'studentBadges': _build_student_badges(leave_students, med_students),
        })
    finally:
        conn.close()


def _load_class_students(conn, class_name, semester):
    sql = '''
        SELECT student_id, chinese_name, english_name
        FROM student_class_cache
        WHERE class_name = ?
    '''
    params = [class_name]
    if semester:
        sql += ' AND semester = ?'
        params.append(semester)
    return conn.execute(sql, params).fetchall()


def _load_leave_rows(conn, class_name, target_date, semester):
    sql = '''
        SELECT
            l.id,
            l.child_id,
            l.type,
            l.start_date,
            l.end_date,
            l.reason,
            l.signature_url,
            l.created_by,
            l.created_at,
            s.chinese_name,
            s.english_name,
            s.class_name
        FROM leave_records l
        JOIN student_class_cache s ON s.student_id = l.child_id
        WHERE s.class_name = ?
          AND l.start_date <= ?
          AND l.end_date >= ?
    '''
    params = [class_name, target_date, target_date]
    if semester:
        sql += ' AND s.semester = ?'
        params.append(semester)
    sql += ' ORDER BY s.chinese_name, l.start_date, l.created_at DESC'
    return conn.execute(sql, params).fetchall()


def _load_med_rows(conn, class_name, target_date, semester):
    sql = '''
        SELECT
            m.id,
            m.child_id,
            m.type,
            m.start_date,
            m.end_date,
            m.reason,
            m.created_by,
            m.created_at,
            m.medication_details,
            s.chinese_name,
            s.english_name,
            s.class_name
        FROM med_records m
        JOIN student_class_cache s ON s.student_id = m.child_id
        WHERE s.class_name = ?
          AND (
                (
                    COALESCE(m.start_date, '') != ''
                    AND COALESCE(m.end_date, '') != ''
                    AND m.start_date <= ?
                    AND m.end_date >= ?
                )
                OR (
                    COALESCE(m.start_date, '') = ''
                    AND COALESCE(m.end_date, '') = ''
                    AND date(m.created_at) = ?
                )
          )
    '''
    params = [class_name, target_date, target_date, target_date]
    if semester:
        sql += ' AND s.semester = ?'
        params.append(semester)
    sql += ' ORDER BY s.chinese_name, m.created_at DESC'
    return conn.execute(sql, params).fetchall()


def _group_leave_students(rows):
    grouped = OrderedDict()
    for row in rows:
        student = _student_summary(row)
        entry = grouped.setdefault(
            student['studentId'],
            {**student, 'recordCount': 0, 'records': []}
        )
        entry['recordCount'] += 1
        entry['records'].append(_leave_record(row))
    return grouped


def _group_med_students(rows):
    grouped = OrderedDict()
    for row in rows:
        student = _student_summary(row)
        entry = grouped.setdefault(
            student['studentId'],
            {**student, 'recordCount': 0, 'records': []}
        )
        entry['recordCount'] += 1
        entry['records'].append(_med_record(row))
    return grouped


def _student_summary(row):
    return {
        'studentId': row['child_id'],
        'chineseName': row['chinese_name'] or '',
        'englishName': row['english_name'] or '',
        'className': row['class_name'] or '',
    }


def _leave_record(row):
    return {
        'id': row['id'],
        'type': row['type'] or '',
        'startDate': row['start_date'] or '',
        'endDate': row['end_date'] or '',
        'reason': row['reason'] or '',
        'createdBy': row['created_by'] or '',
        'createdAt': row['created_at'] or '',
        'signatureUrl': row['signature_url'] or '',
    }


def _med_record(row):
    details = _parse_medication_details(row['medication_details'])
    reason_labels = _med_reason_labels(row, details)
    time_labels = _med_time_labels(details)
    return {
        'id': row['id'],
        'type': row['type'] or '',
        'startDate': row['start_date'] or '',
        'endDate': row['end_date'] or '',
        'reason': row['reason'] or '',
        'createdBy': row['created_by'] or '',
        'createdAt': row['created_at'] or '',
        'reasonSummary': '、'.join(reason_labels),
        'timeSummary': '、'.join(time_labels),
        'dosageSummary': _med_dosage_summary(details),
        'imageUrl': details.get('imageUrl') or '',
        'signatureUrl': details.get('signatureUrl') or '',
    }


def _parse_medication_details(raw):
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _med_reason_labels(row, details):
    reasons = details.get('reasons')
    if isinstance(reasons, list) and reasons:
        return [MED_REASON_LABELS.get(str(reason), str(reason)) for reason in reasons]
    if row['type']:
        return [row['type']]
    if row['reason']:
        return [row['reason']]
    return []


def _med_time_labels(details):
    times = details.get('times')
    if not isinstance(times, list):
        return []
    return [MED_TIME_LABELS.get(str(time), str(time)) for time in times]


def _med_dosage_summary(details):
    dosage = details.get('dosage')
    if not isinstance(dosage, dict):
        return ''
    parts = []
    for dosage_type, value in dosage.items():
        if not isinstance(value, dict):
            continue
        amount = str(value.get('amount') or '').strip()
        if not amount:
            continue
        if dosage_type == 'pills':
            parts.append(f'藥丸 {amount}')
        elif dosage_type == 'powder':
            parts.append(f'藥粉 {amount}')
        elif dosage_type == 'liquid':
            parts.append(f'藥水 {amount}')
        else:
            parts.append(f'{dosage_type} {amount}')
    return '、'.join(parts)


def _build_student_badges(leave_students, med_students):
    student_ids = sorted(set(leave_students.keys()) | set(med_students.keys()))
    badges = {}
    for student_id in student_ids:
        leave_count = leave_students.get(student_id, {}).get('recordCount', 0)
        med_count = med_students.get(student_id, {}).get('recordCount', 0)
        badges[student_id] = {
            'studentId': student_id,
            'leaveCount': leave_count,
            'medCount': med_count,
            'totalCount': leave_count + med_count,
        }
    return badges

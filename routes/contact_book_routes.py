from flask import Blueprint, request, jsonify
from datetime import datetime
import json
from services.data_service import DataService
import os

contact_book_bp = Blueprint('contact_book', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

# Helper to deserialize json fields safely
def load_json(val):
    if not val:
        return None
    try:
        return json.loads(val)
    except:
        return None

def format_record(r, version='original'):
    rec = {
        'date': r['date'],
        'dayOfWeek': r['day_of_week'],
        'status': r['status'],
        'readAt': r['read_at'],
        'signedAt': r['signed_at'],
        'itemsToBring': load_json(r['items_to_bring']),
        'returnedItems': load_json(r['returned_items']) or [],
        'attachedItems': load_json(r['attached_items']) or [],
        'original': {
            'teacher': load_json(r['original_teacher']),
            'parent': load_json(r['original_parent'])
        },
        'redacted': load_json(r['redacted']),
        'comments': load_json(r['comments']) or [],
        'surveyId': r['survey_id']
    }
    
    # Apply versioning overlay (original or redacted)
    if version == 'redacted' and rec['redacted']:
        rec['teacher'] = rec['redacted'].get('teacher')
        rec['parent'] = rec['redacted'].get('parent')
    else:
        rec['teacher'] = rec['original'].get('teacher')
        rec['parent'] = rec['original'].get('parent')
        
    return rec

@contact_book_bp.route('/<student_id>/months', methods=['GET'])
def get_available_months(student_id):
    """Get list of available months for a student's contact book"""
    conn = data_service.get_db()
    try:
        rows = conn.execute('SELECT DISTINCT year, month FROM contact_books WHERE student_id = ? ORDER BY year ASC, month ASC', (student_id,)).fetchall()
        months = [f"{r['year']}-{r['month']:02d}" for r in rows]
        return jsonify(months), 200
    finally:
        conn.close()

@contact_book_bp.route('/<student_id>/<int:year>/<int:month>', methods=['GET'])
def get_contact_book(student_id, year, month):
    version = request.args.get('version', 'original')
    conn = data_service.get_db()
    try:
        rows = conn.execute('SELECT * FROM contact_books WHERE student_id = ? AND year = ? AND month = ? ORDER BY date ASC', (student_id, year, month)).fetchall()
        if not rows:
            # We can return empty records structure like before
            return jsonify({'studentId': student_id, 'year': year, 'month': month, 'records': []}), 200
        
        records = [format_record(r, version) for r in rows]
        
        data = {
            'studentId': student_id,
            'year': year,
            'month': month,
            'records': records,
            'metadata': {'lastModified': rows[0]['last_modified'] if rows and rows[0]['last_modified'] else datetime.now().isoformat()}
        }
        return jsonify(data), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@contact_book_bp.route('/<student_id>/<date>/parent', methods=['PUT'])
def update_parent_entry(student_id, date):
    data = request.json
    year, month, day = map(int, date.split('-'))
    conn = data_service.get_db()
    try:
        row = conn.execute('SELECT original_parent FROM contact_books WHERE student_id = ? AND date = ?', (student_id, date)).fetchone()
        if not row:
            conn.execute('''
                INSERT INTO contact_books (student_id, date, year, month, status, original_parent, last_modified)
                VALUES (?, ?, ?, ?, 'pending_teacher', ?, ?)
            ''', (student_id, date, year, month, json.dumps(data, ensure_ascii=False), datetime.now().isoformat()))
        else:
            conn.execute('UPDATE contact_books SET original_parent = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                         (json.dumps(data, ensure_ascii=False), datetime.now().isoformat(), student_id, date))
        conn.commit()
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@contact_book_bp.route('/<student_id>/<date>/teacher', methods=['PUT'])
def update_teacher_entry(student_id, date):
    data = request.json
    year, month, day = map(int, date.split('-'))
    conn = data_service.get_db()
    try:
        row = conn.execute('SELECT id FROM contact_books WHERE student_id = ? AND date = ?', (student_id, date)).fetchone()
        if not row:
            conn.execute('''
                INSERT INTO contact_books (student_id, date, year, month, status, original_teacher, last_modified)
                VALUES (?, ?, ?, ?, 'completed', ?, ?)
            ''', (student_id, date, year, month, json.dumps(data, ensure_ascii=False), datetime.now().isoformat()))
        else:
            conn.execute('UPDATE contact_books SET original_teacher = ?, status = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                         (json.dumps(data, ensure_ascii=False), 'completed', datetime.now().isoformat(), student_id, date))
        conn.commit()
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@contact_book_bp.route('/<student_id>/latest', methods=['GET'])
def get_latest_records(student_id):
    limit = request.args.get('limit', 10, type=int)
    conn = data_service.get_db()
    try:
        rows = conn.execute('SELECT * FROM contact_books WHERE student_id = ? ORDER BY date DESC LIMIT ?', (student_id, limit)).fetchall()
        records = [format_record(r, 'original') for r in rows]
        return jsonify(records), 200
    finally:
        conn.close()

@contact_book_bp.route('/<student_id>/<date>/read', methods=['PUT'])
def mark_as_read(student_id, date):
    data = request.json or {}
    conn = data_service.get_db()
    try:
        row = conn.execute('SELECT status FROM contact_books WHERE student_id = ? AND date = ?', (student_id, date)).fetchone()
        if not row:
            return jsonify({'error': 'Record not found'}), 404
        
        if row['status'] == 'pending_parent':
            read_at = data.get('readAt') or datetime.now().isoformat()
            conn.execute('UPDATE contact_books SET status = ?, read_at = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                         ('read', read_at, datetime.now().isoformat(), student_id, date))
            conn.commit()
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@contact_book_bp.route('/<student_id>/<date>/sign', methods=['PUT'])
def mark_as_signed(student_id, date):
    data = request.json or {}
    conn = data_service.get_db()
    try:
        row = conn.execute('SELECT original_parent FROM contact_books WHERE student_id = ? AND date = ?', (student_id, date)).fetchone()
        if not row:
            return jsonify({'error': 'Record not found'}), 404
        
        signed_at = data.get('signedAt') or datetime.now().isoformat()
        
        # Update parent note if provided
        new_parent_data = None
        if data.get('note'):
            parent_obj = load_json(row['original_parent']) or {}
            parent_obj['note'] = data['note']
            parent_obj['updatedAt'] = signed_at
            new_parent_data = json.dumps(parent_obj, ensure_ascii=False)
            
            conn.execute('UPDATE contact_books SET status = ?, signed_at = ?, original_parent = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                         ('signed', signed_at, new_parent_data, datetime.now().isoformat(), student_id, date))
        else:
            conn.execute('UPDATE contact_books SET status = ?, signed_at = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                         ('signed', signed_at, datetime.now().isoformat(), student_id, date))
        conn.commit()
        return jsonify({'status': 'signed'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@contact_book_bp.route('/<student_id>/<date>/items-checked', methods=['PUT'])
def update_items_checked(student_id, date):
    data = request.json or {}
    conn = data_service.get_db()
    try:
        row = conn.execute('SELECT items_to_bring FROM contact_books WHERE student_id = ? AND date = ?', (student_id, date)).fetchone()
        if not row:
            return jsonify({'error': 'Record not found'}), 404
        
        items_obj = load_json(row['items_to_bring']) or {}
        items_obj['checkedItems'] = data.get('checkedItems', [])
        items_obj['checkedAt'] = data.get('checkedAt') or datetime.now().isoformat()
        
        conn.execute('UPDATE contact_books SET items_to_bring = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                     (json.dumps(items_obj, ensure_ascii=False), datetime.now().isoformat(), student_id, date))
        conn.commit()
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@contact_book_bp.route('/<student_id>/<date>/comments', methods=['GET', 'POST'])
def handle_comments(student_id, date):
    conn = data_service.get_db()
    try:
        row = conn.execute('SELECT comments FROM contact_books WHERE student_id = ? AND date = ?', (student_id, date)).fetchone()
        if not row:
            return jsonify({'error': 'Record not found'}), 404
            
        comments = load_json(row['comments']) or []
        
        if request.method == 'GET':
            return jsonify(comments), 200
            
        elif request.method == 'POST':
            data = request.json
            if not data or not data.get('content'):
                return jsonify({'error': 'Content is required'}), 400
                
            comment = {
                'senderId': data.get('senderId', 'parent'),
                'name': data.get('name', '家長'),
                'content': data['content'],
                'createdAt': datetime.now().isoformat()
            }
            comments.append(comment)
            
            conn.execute('UPDATE contact_books SET comments = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                         (json.dumps(comments, ensure_ascii=False), datetime.now().isoformat(), student_id, date))
            conn.commit()
            return jsonify(comment), 201
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

from flask import Blueprint, request, jsonify
from datetime import datetime
import json
import threading
from services.data_service import DataService
import os

contact_book_bp = Blueprint('contact_book', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

# Auto-migrate: add edited_by column if missing
def _auto_migrate():
    conn = data_service.get_db()
    try:
        cols = [row[1] for row in conn.execute('PRAGMA table_info(contact_books)').fetchall()]
        if 'edited_by' not in cols:
            conn.execute('ALTER TABLE contact_books ADD COLUMN edited_by TEXT')
            conn.commit()
            print('[Migration] Added edited_by column to contact_books')
    except Exception as e:
        print(f'[Migration] Error: {e}')
    finally:
        conn.close()

_auto_migrate()

# Lazy import to avoid circular imports or missing dependencies
def _get_notifier():
    try:
        from services.send_notification import notify_teachers_new_comment
        return notify_teachers_new_comment
    except Exception as e:
        print(f'[Notification] Failed to import sender: {e}')
        return None


def _get_status_notifier():
    try:
        from services.send_notification import notify_teachers_status_update
        return notify_teachers_status_update
    except Exception as e:
        print(f'[Notification] Failed to import status notifier: {e}')
        return None


def _get_parent_record_notifier():
    try:
        from services.send_notification import notify_parents_new_record
        return notify_parents_new_record
    except Exception as e:
        print(f'[Notification] Failed to import parent record notifier: {e}')
        return None


def _get_parent_comment_notifier():
    try:
        from services.send_notification import notify_parents_new_comment
        return notify_parents_new_comment
    except Exception as e:
        print(f'[Notification] Failed to import parent comment notifier: {e}')
        return None

# Helper to deserialize json fields safely
def load_json(val):
    if not val:
        return None
    try:
        return json.loads(val)
    except:
        return None

DAY_NAMES = ['日', '一', '二', '三', '四', '五', '六']

def format_record(r, version='original'):
    # Compute dayOfWeek from date if not stored
    date_str = r['date']
    stored_dow = r['day_of_week']
    if not stored_dow:
        try:
            y, m, d = map(int, date_str.split('-'))
            dow_index = datetime(y, m, d).weekday()  # 0=Mon ... 6=Sun
            stored_dow = DAY_NAMES[(dow_index + 1) % 7]  # Convert to Chinese
        except:
            stored_dow = ''
    
    # Normalize status: 'completed' means teacher filled but parent hasn't read yet
    status = r['status']
    if status == 'completed':
        status = 'pending_parent'
    
    rec = {
        'date': date_str,
        'dayOfWeek': stored_dow,
        'status': status,
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
        'surveyId': r['survey_id'],
        'editedBy': load_json(r['edited_by']) if r['edited_by'] else None
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
    
    # Extract fields that have their own DB columns (not stored inside teacher JSON)
    survey_id = data.pop('surveyId', None) or None
    
    # Extract editedBy info (teacher identity)
    edited_by_raw = data.pop('editedBy', None)
    if edited_by_raw:
        edited_by_raw['editedAt'] = datetime.now().isoformat()
        edited_by = json.dumps(edited_by_raw, ensure_ascii=False)
    else:
        edited_by = None
    
    # itemsToBring: frontend sends plain array like ["水壺", "餐具"]
    # DB stores as {"items": [...], "checkedItems": [...], "checkedAt": ...}
    raw_items = data.pop('itemsToBring', None)
    if raw_items and isinstance(raw_items, list) and len(raw_items) > 0:
        items_to_bring = json.dumps({'items': raw_items}, ensure_ascii=False)
    else:
        items_to_bring = None
    
    # returnedItems: stored as plain JSON array
    raw_returned = data.pop('returnedItems', None)
    if raw_returned and isinstance(raw_returned, list) and len(raw_returned) > 0:
        returned_items = json.dumps(raw_returned, ensure_ascii=False)
    else:
        returned_items = None
    
    # attachedItems: also stored separately
    raw_attached = data.pop('attachedItems', None)
    attached_items = json.dumps(raw_attached, ensure_ascii=False) if raw_attached else None
    
    conn = data_service.get_db()
    try:
        row = conn.execute('SELECT id FROM contact_books WHERE student_id = ? AND date = ?', (student_id, date)).fetchone()
        if not row:
            conn.execute('''
                INSERT INTO contact_books (student_id, date, year, month, status, original_teacher, 
                    items_to_bring, returned_items, attached_items, survey_id, edited_by, last_modified)
                VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?)
            ''', (student_id, date, year, month, json.dumps(data, ensure_ascii=False),
                  items_to_bring, returned_items, attached_items, survey_id, edited_by, datetime.now().isoformat()))
        else:
            conn.execute('''UPDATE contact_books SET original_teacher = ?, items_to_bring = ?,
                returned_items = ?, attached_items = ?, survey_id = ?, edited_by = ?, status = ?, last_modified = ?
                WHERE student_id = ? AND date = ?''',
                (json.dumps(data, ensure_ascii=False), items_to_bring, returned_items,
                 attached_items, survey_id, edited_by, 'completed', datetime.now().isoformat(), student_id, date))
        conn.commit()

        # Notify parents that teacher has updated the contact book
        student_name = ''
        if edited_by_raw:
            # Try to get student name from editedBy context (not always available)
            student_name = request.json.get('studentName', '')

        def _notify_parent_bg():
            notify = _get_parent_record_notifier()
            if notify:
                notify(data_service, student_id, student_name or student_id, date)
        threading.Thread(target=_notify_parent_bg, daemon=True).start()

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
        
        status_changed = False
        if row['status'] in ('pending_parent', 'completed'):
            read_at = data.get('readAt') or datetime.now().isoformat()
            conn.execute('UPDATE contact_books SET status = ?, read_at = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                         ('read', read_at, datetime.now().isoformat(), student_id, date))
            conn.commit()
            status_changed = True
        
        # Notify teachers via silent FCM push (no toast shown)
        if status_changed:
            student_name = data.get('studentName', student_id)
            def _notify_bg():
                notify = _get_status_notifier()
                if notify:
                    notify(data_service, student_id, student_name, date, 'read')
            threading.Thread(target=_notify_bg, daemon=True).start()
        
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
        
        # Notify teachers via silent FCM push (no toast shown)
        student_name = data.get('studentName', student_id)
        def _notify_bg():
            notify = _get_status_notifier()
            if notify:
                notify(data_service, student_id, student_name, date, 'signed')
        threading.Thread(target=_notify_bg, daemon=True).start()
        
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
            
            now_iso = datetime.now().isoformat()
            comment = {
                'id': f"{student_id}_{date}_{datetime.now().timestamp():.6f}",  # Stable unique ID
                'senderId': data.get('senderId', 'parent'),
                'name': data.get('name', '家長'),
                'cname': data.get('cname', ''),
                'ename': data.get('ename', ''),
                'content': data['content'],
                'createdAt': now_iso
            }
            comments.append(comment)
            
            conn.execute('UPDATE contact_books SET comments = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                         (json.dumps(comments, ensure_ascii=False), now_iso, student_id, date))
            conn.commit()
            
            # Send push notification based on sender role
            sender_name = comment['name']
            content_preview = comment['content']
            student_name = data.get('studentName', student_id)
            sender_role = data.get('senderRole', 'parent')

            def _send_bg():
                if sender_role in ('teacher', 'admin'):
                    # Teacher posted a comment → notify parents of this student
                    notify = _get_parent_comment_notifier()
                    if notify:
                        notify(data_service, student_id, student_name, sender_name, content_preview, date)
                else:
                    # Parent posted a comment → notify teachers/admins
                    notify = _get_notifier()
                    if notify:
                        notify(data_service, student_id, student_name, sender_name, content_preview, date)

            threading.Thread(target=_send_bg, daemon=True).start()
            
            return jsonify(comment), 201
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@contact_book_bp.route('/<student_id>/<date>/comments/<comment_id>', methods=['DELETE'])
def delete_comment(student_id, date, comment_id):
    """
    Delete a single comment identified by comment_id.
    Matching order:
      1. comment['id'] == comment_id         (new comments with server-assigned id)
      2. comment['content'] == comment_id    (old image comments identified by URL)
      3. comment['createdAt'] == comment_id  (legacy fallback by timestamp)
    Returns 200 on success, 404 if the record or comment is not found.
    """
    conn = data_service.get_db()
    try:
        row = conn.execute(
            'SELECT comments FROM contact_books WHERE student_id = ? AND date = ?',
            (student_id, date)
        ).fetchone()
        if not row:
            return jsonify({'error': 'Record not found'}), 404

        comments = load_json(row['comments']) or []
        original_len = len(comments)

        def _matches(c):
            return (
                c.get('id') == comment_id or
                c.get('content') == comment_id or
                c.get('createdAt') == comment_id
            )

        filtered = [c for c in comments if not _matches(c)]

        if len(filtered) == original_len:
            # Nothing was removed
            return jsonify({'error': 'Comment not found'}), 404

        conn.execute(
            'UPDATE contact_books SET comments = ?, last_modified = ? WHERE student_id = ? AND date = ?',
            (json.dumps(filtered, ensure_ascii=False), datetime.now().isoformat(), student_id, date)
        )
        conn.commit()
        return jsonify({'status': 'deleted', 'remaining': len(filtered)}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


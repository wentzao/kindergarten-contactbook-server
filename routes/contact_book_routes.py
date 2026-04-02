from flask import Blueprint, request, jsonify
from datetime import datetime
from urllib.parse import unquote
import json
import threading
import requests as http_requests
from services.data_service import DataService
import os


def _upsert_parent_profile(user_id: str, display_name: str, picture_url: str):
    """Background task: upsert parent_profiles, re-fetching avatar blob if URL changed."""
    if not user_id:
        return
    try:
        conn = data_service.get_db()
        now = datetime.now().isoformat()
        row = conn.execute(
            'SELECT picture_url FROM parent_profiles WHERE user_id = ?', (user_id,)
        ).fetchone()

        fetch_blob = (row is None) or (row['picture_url'] != picture_url and picture_url)
        blob, mime = None, 'image/jpeg'

        if fetch_blob and picture_url:
            try:
                resp = http_requests.get(picture_url, timeout=8)
                if resp.ok:
                    blob = resp.content
                    mime = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0]
            except Exception as e:
                print(f'[ParentProfile] avatar fetch error: {e}')

        if row is None:
            conn.execute(
                'INSERT INTO parent_profiles (user_id, display_name, picture_url, picture_data, picture_mime, updated_at) VALUES (?,?,?,?,?,?)',
                (user_id, display_name, picture_url, blob, mime, now)
            )
        else:
            if fetch_blob and blob:
                conn.execute(
                    'UPDATE parent_profiles SET display_name=?, picture_url=?, picture_data=?, picture_mime=?, updated_at=? WHERE user_id=?',
                    (display_name, picture_url, blob, mime, now, user_id)
                )
            else:
                conn.execute(
                    'UPDATE parent_profiles SET display_name=?, updated_at=? WHERE user_id=?',
                    (display_name, now, user_id)
                )
        conn.commit()
    except Exception as e:
        print(f'[ParentProfile] upsert error: {e}')
    finally:
        try:
            conn.close()
        except Exception:
            pass

contact_book_bp = Blueprint('contact_book', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

# Auto-migrate: add edited_by column if missing
def _auto_migrate():
    conn = None
    try:
        conn = data_service.get_db()
        cols = [row[1] for row in conn.execute('PRAGMA table_info(contact_books)').fetchall()]
        if 'edited_by' not in cols:
            conn.execute('ALTER TABLE contact_books ADD COLUMN edited_by TEXT')
            conn.commit()
            print('[Migration] Added edited_by column to contact_books')
    except Exception as e:
        print(f'[Migration] Error: {e}')
    finally:
        if conn:
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


def _get_comment_deleted_notifier():
    try:
        from services.send_notification import notify_teachers_comment_deleted
        return notify_teachers_comment_deleted
    except Exception as e:
        print(f'[Notification] Failed to import comment-deleted notifier: {e}')
        return None


def _get_parent_comment_deleted_notifier():
    try:
        from services.send_notification import notify_parents_comment_deleted
        return notify_parents_comment_deleted
    except Exception as e:
        print(f'[Notification] Failed to import parent comment-deleted notifier: {e}')
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

def _resolve_teacher_name(user_id, profiles):
    """Resolve userId to {cname, ename} from teacher_profiles cache."""
    if not user_id or not profiles:
        return None
    p = profiles.get(user_id)
    if p:
        return {'userId': user_id, 'cname': p.get('cname', ''), 'ename': p.get('ename', '')}
    return {'userId': user_id, 'cname': '', 'ename': ''}


def _load_teacher_profiles(conn):
    """Load all teacher profiles from DB into a dict."""
    try:
        rows = conn.execute('SELECT user_id, cname, ename FROM teacher_profiles').fetchall()
        return {r['user_id']: {'cname': r['cname'], 'ename': r['ename']} for r in rows}
    except Exception:
        return {}


def format_record(r, version='original', teacher_profiles=None):
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

    status = r['status']

    # Resolve editedBy userId → cname/ename (cache overrides stored names if available)
    edited_by_raw = load_json(r['edited_by'])
    if edited_by_raw and teacher_profiles:
        uid = edited_by_raw.get('userId', '')
        resolved = _resolve_teacher_name(uid, teacher_profiles)
        if resolved:
            if resolved['cname']:
                edited_by_raw['cname'] = resolved['cname']
            if resolved['ename']:
                edited_by_raw['ename'] = resolved['ename']

    # Resolve comment sender names
    comments = load_json(r['comments']) or []
    if teacher_profiles:
        for c in comments:
            sid = c.get('senderId', '')
            if sid and sid != 'parent':
                resolved = _resolve_teacher_name(sid, teacher_profiles)
                if resolved:
                    c['cname'] = resolved['cname']
                    c['ename'] = resolved['ename']

    rec = {
        'date': date_str,
        'dayOfWeek': stored_dow,
        'status': status,
        'readAt': r['read_at'],
        'signedAt': r['signed_at'],
        'notifiedAt': r['notified_at'],
        'itemsToBring': load_json(r['items_to_bring']),
        'returnedItems': load_json(r['returned_items']) or [],
        'attachedItems': load_json(r['attached_items']) or [],
        'original': {
            'teacher': load_json(r['original_teacher']),
            'parent': load_json(r['original_parent'])
        },
        'redacted': load_json(r['redacted']),
        'comments': comments,
        'surveyId': r['survey_id'],
        'editedBy': edited_by_raw,
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
    class_name = request.args.get('className')
    conn = data_service.get_db()
    try:
        profiles = _load_teacher_profiles(conn)
        rows = conn.execute('SELECT * FROM contact_books WHERE student_id = ? AND year = ? AND month = ? ORDER BY date ASC', (student_id, year, month)).fetchall()
        if not rows:
            return jsonify({'studentId': student_id, 'year': year, 'month': month, 'records': []}), 200

        records = [format_record(r, version, profiles) for r in rows]

        # Embed class journal blocks if className is provided
        # Only show journal for dates where THIS STUDENT has been notified (per-student check)
        if class_name:
            journal_map = {}
            journal_rows = conn.execute(
                'SELECT date, content_blocks, edited_by, updated_at FROM class_journals WHERE class_name = ? AND date LIKE ?',
                (class_name, f'{year}-{month:02d}-%')
            ).fetchall()
            for jr in journal_rows:
                j_edited_by = load_json(jr['edited_by'])
                # Enrich journal editedBy from teacher_profiles cache
                if j_edited_by and profiles:
                    uid = j_edited_by.get('userId', '')
                    resolved = _resolve_teacher_name(uid, profiles)
                    if resolved:
                        if resolved['cname']:
                            j_edited_by['cname'] = resolved['cname']
                        if resolved['ename']:
                            j_edited_by['ename'] = resolved['ename']
                journal_map[jr['date']] = {
                    'contentBlocks': load_json(jr['content_blocks']) or [],
                    'editedBy': j_edited_by,
                    'updatedAt': jr['updated_at'],
                }
            for rec in records:
                # Only include journal if student's status is notified/read/signed
                if rec.get('status') in ('notified', 'read', 'signed') and rec['date'] in journal_map:
                    rec['classJournal'] = journal_map[rec['date']]
                else:
                    rec['classJournal'] = None

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
                VALUES (?, ?, ?, ?, 'draft', ?, ?)
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
    
    # Extract editedBy — store only userId + timestamp (names resolved via teacher_profiles)
    edited_by_raw = data.pop('editedBy', None)
    if edited_by_raw:
        edited_by = json.dumps({
            'userId': edited_by_raw.get('userId', ''),
            'editedAt': datetime.now().isoformat(),
        }, ensure_ascii=False)
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
                VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)
            ''', (student_id, date, year, month, json.dumps(data, ensure_ascii=False),
                  items_to_bring, returned_items, attached_items, survey_id, edited_by, datetime.now().isoformat()))
        else:
            # Don't downgrade status if already notified/read/signed
            current = conn.execute('SELECT status FROM contact_books WHERE student_id = ? AND date = ?', (student_id, date)).fetchone()
            new_status = current['status'] if current and current['status'] in ('notified', 'read', 'signed') else 'draft'
            conn.execute('''UPDATE contact_books SET original_teacher = ?, items_to_bring = ?,
                returned_items = ?, attached_items = ?, survey_id = ?, edited_by = ?, status = ?, last_modified = ?
                WHERE student_id = ? AND date = ?''',
                (json.dumps(data, ensure_ascii=False), items_to_bring, returned_items,
                 attached_items, survey_id, edited_by, new_status, datetime.now().isoformat(), student_id, date))
        conn.commit()

        # Note: parent notification is no longer sent immediately on teacher save.
        # Teachers use the batch notification endpoint (/api/notifications/send-batch)
        # or scheduled notifications to notify parents after completing the whole class.

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
        profiles = _load_teacher_profiles(conn)
        rows = conn.execute('SELECT * FROM contact_books WHERE student_id = ? ORDER BY date DESC LIMIT ?', (student_id, limit)).fetchall()
        records = [format_record(r, 'original', profiles) for r in rows]
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
        if row['status'] == 'notified':
            read_at = data.get('readAt') or datetime.now().isoformat()
            conn.execute('UPDATE contact_books SET status = ?, read_at = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                         ('read', read_at, datetime.now().isoformat(), student_id, date))
            conn.commit()
            status_changed = True
        
        # Notify teachers via silent FCM push (no toast shown)
        if status_changed:
            student_name = data.get('studentName') or student_id
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
            # Resolve teacher names in comments
            profiles = _load_teacher_profiles(conn)
            for c in comments:
                sid = c.get('senderId', '')
                if sid and sid != 'parent' and profiles:
                    tp = profiles.get(sid, {})
                    c['cname'] = tp.get('cname', '')
                    c['ename'] = tp.get('ename', '')
            return jsonify(comments), 200
            
        elif request.method == 'POST':
            data = request.json
            if not data or not data.get('content'):
                return jsonify({'error': 'Content is required'}), 400
            
            now_iso = datetime.now().isoformat()
            sender_role = data.get('senderRole', 'parent')
            # For teachers: store userId as senderId (names resolved on read via teacher_profiles)
            # For parents: store 'parent' as senderId, name from data
            sender_id = data.get('senderId', 'parent')
            comment = {
                'id': f"{student_id}_{date}_{datetime.now().timestamp():.6f}",
                'senderId': sender_id,
                'senderRole': sender_role,
                'content': data['content'],
                'createdAt': now_iso,
            }
            # Only store name/userId for parents (teacher names resolved dynamically)
            if sender_role == 'parent':
                comment['name'] = data.get('name', '家長')
                if data.get('userId'):
                    comment['userId'] = data['userId']
                    # Upsert profile in background (fetches blob if pictureUrl changed)
                    threading.Thread(
                        target=_upsert_parent_profile,
                        args=(data['userId'], data.get('name', '家長'), data.get('pictureUrl', '')),
                        daemon=True
                    ).start()
            comments.append(comment)

            conn.execute('UPDATE contact_books SET comments = ?, last_modified = ? WHERE student_id = ? AND date = ?',
                         (json.dumps(comments, ensure_ascii=False), now_iso, student_id, date))
            conn.commit()

            # Resolve teacher name for notification push text
            profiles = _load_teacher_profiles(conn)
            if sender_role in ('teacher', 'admin'):
                tp = profiles.get(sender_id, {})
                sender_display = tp.get('ename') or tp.get('cname') or '老師'
            else:
                sender_display = data.get('name', '家長')

            # Enrich returned comment with resolved names
            if sender_role in ('teacher', 'admin'):
                tp = profiles.get(sender_id, {})
                comment['cname'] = tp.get('cname', '')
                comment['ename'] = tp.get('ename', '')

            content_preview = comment['content']
            student_name = data.get('studentName') or student_id

            def _send_bg():
                if sender_role in ('teacher', 'admin'):
                    notify = _get_parent_comment_notifier()
                    if notify:
                        notify(data_service, student_id, student_name, sender_display, content_preview, date)
                else:
                    notify = _get_notifier()
                    if notify:
                        notify(data_service, student_id, student_name, sender_display, content_preview, date)

            threading.Thread(target=_send_bg, daemon=True).start()

            return jsonify(comment), 201
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@contact_book_bp.route('/<student_id>/<date>/comments/<comment_id>', methods=['PATCH'])
def edit_comment(student_id, date, comment_id):
    """
    Edit the content of a single comment identified by comment_id.
    Matching order: comment['id'], comment['content'], comment['createdAt'].
    Body: { "content": "new text" }
    Returns the updated comment on success, 404 if not found.
    """
    comment_id = unquote(comment_id)
    body = request.get_json()
    if not body or 'content' not in body:
        return jsonify({'error': 'Missing content'}), 400
    new_content = body['content'].strip()
    if not new_content:
        return jsonify({'error': 'Content cannot be empty'}), 400

    conn = data_service.get_db()
    try:
        row = conn.execute(
            'SELECT comments FROM contact_books WHERE student_id = ? AND date = ?',
            (student_id, date)
        ).fetchone()
        if not row:
            return jsonify({'error': 'Record not found'}), 404

        comments = load_json(row['comments']) or []

        def _matches(c):
            return (
                c.get('id') == comment_id or
                c.get('content') == comment_id or
                c.get('createdAt') == comment_id
            )

        updated_comment = None
        updated_comments = []
        for c in comments:
            if _matches(c) and updated_comment is None:
                c = dict(c)
                c['content'] = new_content
                c['updatedAt'] = datetime.now().isoformat()
                updated_comment = c
            updated_comments.append(c)

        if updated_comment is None:
            return jsonify({'error': 'Comment not found'}), 404

        conn.execute(
            'UPDATE contact_books SET comments = ?, last_modified = ? WHERE student_id = ? AND date = ?',
            (json.dumps(updated_comments, ensure_ascii=False), datetime.now().isoformat(), student_id, date)
        )
        conn.commit()
        return jsonify(updated_comment), 200

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
    # Decode percent-encoding (e.g. %2F → /) for URL-as-id fallback
    comment_id = unquote(comment_id)
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

        deleted = [c for c in comments if _matches(c)]
        filtered = [c for c in comments if not _matches(c)]

        if len(filtered) == original_len:
            # Nothing was removed
            return jsonify({'error': 'Comment not found'}), 404

        # Capture info before committing — needed for notification and cache eviction
        deleted_content = deleted[0].get('content', '') if deleted else ''
        deleted_sender_name = deleted[0].get('name', '家長') if deleted else '家長'
        deleted_sender_role = deleted[0].get('senderRole', 'parent') if deleted else 'parent'

        conn.execute(
            'UPDATE contact_books SET comments = ?, last_modified = ? WHERE student_id = ? AND date = ?',
            (json.dumps(filtered, ensure_ascii=False), datetime.now().isoformat(), student_id, date)
        )
        conn.commit()

        # Notify the other party so their chat view refreshes and evicts the cached image.
        # Teacher deleted → notify parents (silent). Parent deleted → notify teachers (visible).
        def _send_bg():
            if deleted_sender_role == 'teacher':
                notify = _get_parent_comment_deleted_notifier()
                if notify:
                    notify(data_service, student_id, date, deleted_image_url)
            else:
                notify = _get_comment_deleted_notifier()
                if notify:
                    notify(data_service, student_id, date, deleted_content, deleted_sender_name)
        threading.Thread(target=_send_bg, daemon=True).start()

        return jsonify({'status': 'deleted', 'remaining': len(filtered)}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ── Batch endpoints for journal editor ──

@contact_book_bp.route('/batch/<class_name>/<date>/teacher', methods=['PUT'])
def batch_save_teacher(class_name, date):
    """Batch save teacher entries for multiple students (from journal editor auto-save).
    Sets status to 'draft' (not visible to parents until notified)."""
    body = request.get_json()
    if not body or 'students' not in body:
        return jsonify({'error': 'Missing students data'}), 400

    students_data = body['students']
    edited_by_raw = body.get('editedBy')
    last_known_modified = body.get('lastModified', {})  # { studentId: "ISO timestamp" }
    edited_by = None
    if edited_by_raw:
        edited_by = json.dumps({
            'userId': edited_by_raw.get('userId', ''),
            'cname': edited_by_raw.get('cname', ''),
            'ename': edited_by_raw.get('ename', ''),
            'editedAt': datetime.now().isoformat(),
        }, ensure_ascii=False)

    year, month, _day = map(int, date.split('-'))
    conn = data_service.get_db()
    try:
        saved_count = 0
        conflicts = {}
        for student_id, note_data in students_data.items():
            # Optimistic lock: check per-student lastModified
            known = last_known_modified.get(student_id)
            if known:
                row_check = conn.execute(
                    'SELECT last_modified, edited_by FROM contact_books WHERE student_id = ? AND date = ?',
                    (student_id, date)
                ).fetchone()
                if row_check and row_check['last_modified'] and row_check['last_modified'] != known:
                    conflicts[student_id] = {
                        'serverModified': row_check['last_modified'],
                        'editedBy': json.loads(row_check['edited_by']) if row_check['edited_by'] else None,
                    }
                    continue  # Skip this student, don't overwrite

            # Extract fields that have their own DB columns
            raw_items = note_data.pop('itemsToBring', None)
            if raw_items and isinstance(raw_items, list) and len(raw_items) > 0:
                items_to_bring = json.dumps({'items': raw_items}, ensure_ascii=False)
            else:
                items_to_bring = None

            raw_returned = note_data.pop('returnedItems', None)
            if raw_returned and isinstance(raw_returned, list) and len(raw_returned) > 0:
                returned_items = json.dumps(raw_returned, ensure_ascii=False)
            else:
                returned_items = None

            survey_id = note_data.pop('surveyId', None) or None
            teacher_json = json.dumps(note_data, ensure_ascii=False)

            row = conn.execute(
                'SELECT id, status FROM contact_books WHERE student_id = ? AND date = ?',
                (student_id, date)
            ).fetchone()

            if not row:
                conn.execute('''
                    INSERT INTO contact_books (student_id, date, year, month, status, original_teacher,
                        items_to_bring, returned_items, survey_id, edited_by, last_modified)
                    VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)
                ''', (student_id, date, year, month, teacher_json,
                      items_to_bring, returned_items, survey_id, edited_by, datetime.now().isoformat()))
            else:
                # Don't downgrade status if already notified/read/signed
                current_status = row['status']
                new_status = current_status if current_status in ('notified', 'read', 'signed') else 'draft'
                conn.execute('''
                    UPDATE contact_books SET original_teacher = ?, items_to_bring = ?,
                        returned_items = ?, survey_id = ?, edited_by = ?, status = ?, last_modified = ?
                    WHERE student_id = ? AND date = ?
                ''', (teacher_json, items_to_bring, returned_items, survey_id,
                      edited_by, new_status, datetime.now().isoformat(), student_id, date))
            saved_count += 1

        conn.commit()

        # Send silent "data_updated" notification for student notes
        if saved_count > 0:
            saved_ids = [sid for sid in students_data.keys() if sid not in conflicts]
            def _notify():
                try:
                    from services.send_notification import send_to_role
                    notify_data = {
                        'type': 'data_updated',
                        'dataType': 'student_notes',
                        'className': class_name,
                        'date': date,
                        'studentIds': json.dumps(saved_ids),
                    }
                    send_to_role(data_service, 'teacher', '', '', notify_data)
                    send_to_role(data_service, 'admin', '', '', notify_data)
                except Exception as e:
                    print(f'[ContactBook] data_updated notification error: {e}')
            threading.Thread(target=_notify, daemon=True).start()

        result = {'status': 'saved', 'count': saved_count}
        if conflicts:
            result['conflicts'] = conflicts
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@contact_book_bp.route('/admin/clear-date/<date>', methods=['DELETE'])
def admin_clear_date(date):
    """Admin: delete all contact_books and class_journals for a given date."""
    conn = data_service.get_db()
    try:
        cb = conn.execute('DELETE FROM contact_books WHERE date = ?', (date,)).rowcount
        cj = conn.execute('DELETE FROM class_journals WHERE date = ?', (date,)).rowcount
        conn.commit()
        return jsonify({'deleted': {'contactBooks': cb, 'classJournals': cj}}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@contact_book_bp.route('/class/<class_name>/<date>/teacher', methods=['GET'])
def get_class_date_teacher(class_name, date):
    """Get all students' contact_book records for a class+date (journal editor load).
    Returns map: { studentId: { original_teacher fields } }"""
    conn = data_service.get_db()
    try:
        # Since contact_books doesn't store class_name, fetch ALL records for this date
        # and let the frontend filter by its student list.
        rows = conn.execute(
            'SELECT student_id, original_teacher, items_to_bring, returned_items, survey_id FROM contact_books WHERE date = ?',
            (date,)
        ).fetchall()

        result = {}
        for r in rows:
            teacher_data = load_json(r['original_teacher']) or {}
            items = load_json(r['items_to_bring'])
            if items and 'items' in items:
                teacher_data['itemsToBring'] = items['items']
            returned = load_json(r['returned_items'])
            if returned:
                teacher_data['returnedItems'] = returned
            if r['survey_id']:
                teacher_data['surveyId'] = r['survey_id']
            result[r['student_id']] = teacher_data

        return jsonify(result), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

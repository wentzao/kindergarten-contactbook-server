"""Contact book routes — Schema v2.

Key change from v1:
  • Parent-visibility is OWNED by class_notification_grants, NOT by
    contact_books.status.
  • contact_books is now a sparse table — only rows that have actual
    content (teacher note, parent read/sign, comments, ...) exist.
  • Parent read path joins grants → class_journals + contact_books.
"""
from flask import Blueprint, request, jsonify
from datetime import datetime
from urllib.parse import unquote
import json
import threading
import os
import requests as http_requests

from services.data_service import DataService
from services.push_outbox_service import (
    EVENT_CONTACT_BOOK_PARENT_COMMENT,
    EVENT_CONTACT_BOOK_PARENT_COMMENT_DELETED,
    EVENT_CONTACT_BOOK_TEACHER_COMMENT,
    EVENT_CONTACT_BOOK_TEACHER_COMMENT_DELETED,
    EVENT_CONTACT_BOOK_TEACHER_STATUS,
    EVENT_TEACHER_USER_IDS_PUSH,
    enqueue_push_job,
    start_push_outbox_worker,
)
from services.contact_book_teacher_payload import (
    merge_existing_visible_content_if_empty,
    normalize_teacher_payload,
)
from services import notification_grant_service as grant_service


contact_book_bp = Blueprint('contact_book', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

DAY_NAMES = ['日', '一', '二', '三', '四', '五', '六']

# Derived "status" exposed to clients (computed, not stored in DB).
DERIVED_NOTIFIED = 'notified'   # grant exists, parent hasn't read
DERIVED_READ = 'read'           # parent has read
DERIVED_SIGNED = 'signed'       # parent has signed


# ──────────────────────────────────────────────────────────
# Background worker startup
# ──────────────────────────────────────────────────────────
def start_contact_book_background_workers():
    grant_service.start_scheduled_notification_worker(data_service)
    start_push_outbox_worker(data_service)


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def load_json(val):
    if not val:
        return None
    try:
        return json.loads(val)
    except Exception:
        return None


def _is_truthy_flag(value):
    if value is None:
        return False
    return str(value).strip().lower() in ('1', 'true', 'yes', 'y', 'on')


def _day_of_week(date_str):
    try:
        y, m, d = map(int, date_str.split('-'))
        dow_index = datetime(y, m, d).weekday()  # 0=Mon ... 6=Sun
        return DAY_NAMES[(dow_index + 1) % 7]
    except Exception:
        return ''


def _resolve_teacher_name(user_id, profiles):
    if not user_id or not profiles:
        return None
    p = profiles.get(user_id)
    if p:
        return {'userId': user_id, 'cname': p.get('cname', ''), 'ename': p.get('ename', '')}
    return {'userId': user_id, 'cname': '', 'ename': ''}


def _load_teacher_profiles(conn):
    try:
        rows = conn.execute('SELECT user_id, cname, ename FROM teacher_profiles').fetchall()
        return {r['user_id']: {'cname': r['cname'], 'ename': r['ename']} for r in rows}
    except Exception:
        return {}


def _enrich_edited_by(edited_by_raw, profiles):
    if edited_by_raw and profiles:
        uid = edited_by_raw.get('userId', '')
        resolved = _resolve_teacher_name(uid, profiles)
        if resolved:
            if resolved['cname']:
                edited_by_raw['cname'] = resolved['cname']
            if resolved['ename']:
                edited_by_raw['ename'] = resolved['ename']
    return edited_by_raw


def _enrich_comments(comments, profiles):
    if not comments or not profiles:
        return comments
    for c in comments:
        sid = c.get('senderId', '')
        if sid and sid != 'parent':
            resolved = _resolve_teacher_name(sid, profiles)
            if resolved:
                c['cname'] = resolved['cname']
                c['ename'] = resolved['ename']
    return comments


def _derived_status(read_at, signed_at, has_grant):
    if signed_at:
        return DERIVED_SIGNED
    if read_at:
        return DERIVED_READ
    if has_grant:
        return DERIVED_NOTIFIED
    return 'draft'


def _row_to_record(row, profiles=None, has_grant=False, grant_notified_at=None,
                   class_journal=None, scheduled_notification=None):
    """Convert one contact_books row → parent/teacher facing record dict.
    `row` may be None (= grant exists but no individual data yet).
    `grant_notified_at` — populate `notifiedAt` so clients (iOS teacher app
    especially) can render "已通知" UI without a second grants lookup."""
    if row:
        date_str = row['date']
        read_at = row['read_at']
        signed_at = row['signed_at']
        original_teacher = normalize_teacher_payload(load_json(row['original_teacher'])) or None
        original_parent = load_json(row['original_parent'])
        edited_by_raw = _enrich_edited_by(load_json(row['edited_by']), profiles)
        comments = _enrich_comments(load_json(row['comments']) or [], profiles)
        items_to_bring = load_json(row['items_to_bring'])
        returned_items = load_json(row['returned_items']) or []
        attached_items = load_json(row['attached_items']) or []
        survey_id = row['survey_id']
        parent_signature_url = row['parent_signature_url']
    else:
        # placeholder record — grant exists, no row yet
        date_str = None  # caller supplies it
        read_at = None
        signed_at = None
        original_teacher = None
        original_parent = None
        edited_by_raw = None
        comments = []
        items_to_bring = None
        returned_items = []
        attached_items = []
        survey_id = None
        parent_signature_url = None

    rec = {
        'date': date_str,
        'dayOfWeek': _day_of_week(date_str) if date_str else '',
        'status': _derived_status(read_at, signed_at, has_grant),
        'notifiedAt': grant_notified_at if has_grant else None,
        'readAt': read_at,
        'signedAt': signed_at,
        'parentSignatureUrl': parent_signature_url,
        'itemsToBring': items_to_bring,
        'returnedItems': returned_items,
        'attachedItems': attached_items,
        'comments': comments,
        'surveyId': survey_id,
        'editedBy': edited_by_raw,
        'teacher': original_teacher,
        'parent': original_parent,
        'classJournal': class_journal,
        'scheduledNotification': scheduled_notification,
    }
    return rec


def _build_journal_map(conn, class_name, date_filter_like, profiles):
    """Return {date: classJournal dict} for a class within a date-range."""
    if not class_name:
        return {}
    rows = conn.execute(
        '''
        SELECT date, content_blocks, edited_by, updated_at, semester
        FROM class_journals
        WHERE class_name = ? AND date LIKE ?
        ''',
        (class_name, date_filter_like),
    ).fetchall()
    out = {}
    for r in rows:
        out[r['date']] = {
            'semester': r['semester'],
            'contentBlocks': load_json(r['content_blocks']) or [],
            'editedBy': _enrich_edited_by(load_json(r['edited_by']), profiles),
            'updatedAt': r['updated_at'],
        }
    return out


def _build_schedule_map(conn, class_name, date_filter_like):
    """Return {date: scheduled_notification dict} for a class within a date-range."""
    if not class_name:
        return {}
    rows = conn.execute(
        '''
        SELECT * FROM scheduled_class_notifications
        WHERE class_name = ? AND date LIKE ? AND status = ?
        ORDER BY send_at DESC, id DESC
        ''',
        (class_name, date_filter_like, grant_service.SCHEDULE_PENDING),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r['date'], grant_service.serialize_scheduled_notification(r))
    return out


# ──────────────────────────────────────────────────────────
# Parent profile upsert (used by addComment)
# ──────────────────────────────────────────────────────────

def _upsert_parent_profile(user_id, display_name, picture_url):
    if not user_id:
        return
    conn = None
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
                (user_id, display_name, picture_url, blob, mime, now),
            )
        else:
            if fetch_blob and blob:
                conn.execute(
                    'UPDATE parent_profiles SET display_name=?, picture_url=?, picture_data=?, picture_mime=?, updated_at=? WHERE user_id=?',
                    (display_name, picture_url, blob, mime, now, user_id),
                )
            else:
                conn.execute(
                    'UPDATE parent_profiles SET display_name=?, updated_at=? WHERE user_id=?',
                    (display_name, now, user_id),
                )
        conn.commit()
    except Exception as e:
        print(f'[ParentProfile] upsert error: {e}')
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────
# UPSERT helper for contact_books (sparse INSERT)
# ──────────────────────────────────────────────────────────

def _ensure_contact_book_row(conn, student_id, date_key):
    """Insert an empty row if it doesn't exist. Returns the row id and existed flag."""
    year, month, _ = map(int, date_key.split('-'))
    row = conn.execute(
        'SELECT id FROM contact_books WHERE student_id = ? AND date = ?',
        (student_id, date_key),
    ).fetchone()
    if row:
        return row['id'], True
    now = datetime.now().isoformat()
    cursor = conn.execute(
        '''
        INSERT INTO contact_books (student_id, date, year, month, last_modified)
        VALUES (?, ?, ?, ?, ?)
        ''',
        (student_id, date_key, year, month, now),
    )
    return cursor.lastrowid, False


# ──────────────────────────────────────────────────────────
# GET /<student_id>/months
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/<student_id>/months', methods=['GET'])
def get_available_months(student_id):
    """List months that have anything visible to this student.
    Parent path (default): unions months from `class_notification_grants`
    (where student is in student_ids) with months from `contact_books`
    rows that have read_at/signed_at/comments. That way past signed months
    show up too.
    Teacher path (`?includeUnpublished=true`): all months that have ANY
    contact_books row for the student."""
    class_name = request.args.get('className')
    include_unpublished = _is_truthy_flag(request.args.get('includeUnpublished'))
    conn = data_service.get_db()
    try:
        months = set()
        if include_unpublished:
            rows = conn.execute(
                'SELECT DISTINCT year, month FROM contact_books WHERE student_id = ?',
                (student_id,),
            ).fetchall()
            for r in rows:
                months.add(f"{r['year']}-{r['month']:02d}")
        else:
            # 1. From grants (parent-visible dates)
            grant_rows = grant_service.get_visible_grants_for_student(
                conn, student_id, class_name=class_name,
            )
            for g in grant_rows:
                months.add(g['date'][:7])
            # 2. Also include months with any read/signed/comments row,
            #    so signed history is reachable even if a grant was later cancelled.
            rows = conn.execute(
                '''
                SELECT DISTINCT year, month FROM contact_books
                WHERE student_id = ?
                  AND (read_at IS NOT NULL OR signed_at IS NOT NULL OR comments IS NOT NULL)
                ''',
                (student_id,),
            ).fetchall()
            for r in rows:
                months.add(f"{r['year']}-{r['month']:02d}")
        return jsonify(sorted(months)), 200
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# GET /<student_id>/<year>/<month>
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/<student_id>/<int:year>/<int:month>', methods=['GET'])
def get_contact_book(student_id, year, month):
    """Return all records for (student, year, month).

    Parent path: keyed by class_notification_grants — every date the student
    can view appears in records[], even if the contact_books row is missing
    or the class journal is missing.

    Teacher path (?includeUnpublished=true): keyed by contact_books rows —
    same as before.
    """
    class_name = request.args.get('className')
    include_unpublished = _is_truthy_flag(request.args.get('includeUnpublished'))
    date_like = f'{year}-{month:02d}-%'

    conn = data_service.get_db()
    try:
        profiles = _load_teacher_profiles(conn)

        # Fetch row map (date → contact_books row) for whichever dates we end up listing
        all_rows = conn.execute(
            'SELECT * FROM contact_books WHERE student_id = ? AND year = ? AND month = ?',
            (student_id, year, month),
        ).fetchall()
        row_map = {r['date']: r for r in all_rows}

        # Determine the date list + collect grant timestamps for each date
        grant_notified_at_by_date = {}
        if include_unpublished:
            dates = sorted(row_map.keys())
            grant_dates = set()
            if class_name:
                grant_rows = grant_service.get_visible_grants_for_student(
                    conn, student_id, class_name=class_name, date_like=date_like,
                )
                grant_dates = {g['date'] for g in grant_rows}
                grant_notified_at_by_date = {g['date']: g['notified_at'] for g in grant_rows}
        else:
            grant_rows = grant_service.get_visible_grants_for_student(
                conn, student_id, class_name=class_name, date_like=date_like,
            ) if class_name else []
            grant_dates = {g['date'] for g in grant_rows}
            grant_notified_at_by_date = {g['date']: g['notified_at'] for g in grant_rows}
            # Also include dates with parent action (read/signed/comments) — keeps
            # past signed records visible even if the grant was later cancelled.
            action_dates = {
                r['date'] for r in all_rows
                if r['read_at'] or r['signed_at'] or r['comments']
            }
            dates = sorted(grant_dates | action_dates)

        journal_map = _build_journal_map(conn, class_name, date_like, profiles)
        schedule_map = _build_schedule_map(conn, class_name, date_like)

        records = []
        for date_str in dates:
            row = row_map.get(date_str)
            has_grant = date_str in grant_dates
            journal = journal_map.get(date_str) if (has_grant or include_unpublished) else None
            rec = _row_to_record(
                row, profiles=profiles, has_grant=has_grant,
                grant_notified_at=grant_notified_at_by_date.get(date_str),
                class_journal=journal,
                scheduled_notification=schedule_map.get(date_str),
            )
            rec['date'] = date_str
            rec['dayOfWeek'] = _day_of_week(date_str)
            records.append(rec)

        last_modified_values = [r['last_modified'] for r in all_rows if r['last_modified']]
        last_modified = max(last_modified_values) if last_modified_values else datetime.now().isoformat()

        return jsonify({
            'studentId': student_id,
            'year': year,
            'month': month,
            'records': records,
            'metadata': {'lastModified': last_modified},
        }), 200
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# GET /<student_id>/latest
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/<student_id>/latest', methods=['GET'])
def get_latest_records(student_id):
    """Return the N latest parent-visible records for a student.
    Parent path uses grants; teacher path uses contact_books rows."""
    limit = max(1, min(request.args.get('limit', 10, type=int), 100))
    class_name = request.args.get('className')
    include_unpublished = _is_truthy_flag(request.args.get('includeUnpublished'))

    conn = data_service.get_db()
    try:
        profiles = _load_teacher_profiles(conn)

        if include_unpublished:
            rows = conn.execute(
                'SELECT * FROM contact_books WHERE student_id = ? ORDER BY date DESC LIMIT ?',
                (student_id, limit),
            ).fetchall()
            records = [
                _row_to_record(
                    r, profiles=profiles,
                    has_grant=False,
                    class_journal=None,
                    scheduled_notification=None,
                ) for r in rows
            ]
        else:
            grant_rows = grant_service.get_visible_grants_for_student(
                conn, student_id, class_name=class_name,
            )
            # latest grants first
            grant_rows = sorted(grant_rows, key=lambda g: g['date'], reverse=True)[:limit]
            dates = [g['date'] for g in grant_rows]
            grant_dates = set(dates)
            grant_notified_at_by_date = {g['date']: g['notified_at'] for g in grant_rows}
            row_map = {}
            if dates:
                placeholders = ','.join('?' for _ in dates)
                row_query = conn.execute(
                    f'SELECT * FROM contact_books WHERE student_id = ? AND date IN ({placeholders})',
                    (student_id, *dates),
                ).fetchall()
                row_map = {r['date']: r for r in row_query}
            journal_map = {}
            if class_name and dates:
                jrows = conn.execute(
                    f'SELECT date, content_blocks, edited_by, updated_at, semester '
                    f'FROM class_journals WHERE class_name = ? AND date IN ({placeholders})',
                    (class_name, *dates),
                ).fetchall()
                for jr in jrows:
                    journal_map[jr['date']] = {
                        'semester': jr['semester'],
                        'contentBlocks': load_json(jr['content_blocks']) or [],
                        'editedBy': _enrich_edited_by(load_json(jr['edited_by']), profiles),
                        'updatedAt': jr['updated_at'],
                    }
            records = []
            for date_str in dates:
                rec = _row_to_record(
                    row_map.get(date_str), profiles=profiles,
                    has_grant=date_str in grant_dates,
                    grant_notified_at=grant_notified_at_by_date.get(date_str),
                    class_journal=journal_map.get(date_str),
                    scheduled_notification=None,
                )
                rec['date'] = date_str
                rec['dayOfWeek'] = _day_of_week(date_str)
                records.append(rec)
        return jsonify(records), 200
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# PUT /<student_id>/<date>/parent
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/<student_id>/<date>/parent', methods=['PUT'])
def update_parent_entry(student_id, date):
    data = request.get_json() or {}
    conn = data_service.get_db()
    try:
        _ensure_contact_book_row(conn, student_id, date)
        conn.execute(
            'UPDATE contact_books SET original_parent = ?, last_modified = ? '
            'WHERE student_id = ? AND date = ?',
            (json.dumps(data, ensure_ascii=False), datetime.now().isoformat(), student_id, date),
        )
        conn.commit()
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# PUT /<student_id>/<date>/teacher  (single-student save)
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/<student_id>/<date>/teacher', methods=['PUT'])
def update_teacher_entry(student_id, date):
    """Save teacher personal note. No longer touches any 'status' field —
    visibility is determined solely by class_notification_grants."""
    data = dict(request.get_json() or {})
    year, month, _day = map(int, date.split('-'))

    survey_id = data.pop('surveyId', None) or None

    edited_by_raw = data.pop('editedBy', None)
    if edited_by_raw:
        edited_by = json.dumps({
            'userId': edited_by_raw.get('userId', ''),
            'editedAt': datetime.now().isoformat(),
        }, ensure_ascii=False)
    else:
        edited_by = None

    raw_items = data.pop('itemsToBring', None)
    items_to_bring = (
        json.dumps({'items': raw_items}, ensure_ascii=False)
        if isinstance(raw_items, list) and raw_items else None
    )

    raw_returned = data.pop('returnedItems', None)
    returned_items = (
        json.dumps(raw_returned, ensure_ascii=False)
        if isinstance(raw_returned, list) and raw_returned else None
    )

    raw_attached = data.pop('attachedItems', None)
    attached_items = json.dumps(raw_attached, ensure_ascii=False) if raw_attached else None

    data = normalize_teacher_payload(data)
    conn = data_service.get_db()
    try:
        row = conn.execute(
            '''SELECT id, original_teacher, items_to_bring, returned_items,
                      attached_items, survey_id
               FROM contact_books WHERE student_id = ? AND date = ?''',
            (student_id, date),
        ).fetchone()
        now = datetime.now().isoformat()
        if not row:
            conn.execute(
                '''
                INSERT INTO contact_books (
                    student_id, date, year, month, original_teacher,
                    items_to_bring, returned_items, attached_items, survey_id,
                    edited_by, last_modified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (student_id, date, year, month, json.dumps(data, ensure_ascii=False),
                 items_to_bring, returned_items, attached_items, survey_id, edited_by, now),
            )
        else:
            merged, preserved = merge_existing_visible_content_if_empty(
                data,
                load_json(row['original_teacher']) or {},
                items_to_bring=raw_items,
                returned_items=raw_returned,
                attached_items=raw_attached,
                survey_id=survey_id,
            )
            if preserved:
                if items_to_bring is None:
                    items_to_bring = row['items_to_bring']
                if returned_items is None:
                    returned_items = row['returned_items']
                if attached_items is None:
                    attached_items = row['attached_items']
                if survey_id is None:
                    survey_id = row['survey_id']
            conn.execute(
                '''UPDATE contact_books SET original_teacher = ?, items_to_bring = ?,
                   returned_items = ?, attached_items = ?, survey_id = ?,
                   edited_by = ?, last_modified = ?
                   WHERE student_id = ? AND date = ?''',
                (json.dumps(merged, ensure_ascii=False), items_to_bring,
                 returned_items, attached_items, survey_id, edited_by, now,
                 student_id, date),
            )
        conn.commit()
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# PUT /<student_id>/<date>/read
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/<student_id>/<date>/read', methods=['PUT'])
def mark_as_read(student_id, date):
    """Parent acknowledges they've seen the contact book.
    Auto-creates a contact_books row if none exists (sparse-table)."""
    data = request.get_json() or {}
    conn = data_service.get_db()
    try:
        _ensure_contact_book_row(conn, student_id, date)
        row = conn.execute(
            'SELECT read_at, signed_at FROM contact_books WHERE student_id = ? AND date = ?',
            (student_id, date),
        ).fetchone()
        # Don't downgrade: if already signed, leave it alone
        if row['signed_at'] or row['read_at']:
            return jsonify({'status': 'noop'}), 200

        read_at = data.get('readAt') or datetime.now().isoformat()
        conn.execute(
            'UPDATE contact_books SET read_at = ?, last_modified = ? '
            'WHERE student_id = ? AND date = ?',
            (read_at, datetime.now().isoformat(), student_id, date),
        )
        student_name = data.get('studentName') or student_id
        enqueue_push_job(
            conn,
            EVENT_CONTACT_BOOK_TEACHER_STATUS,
            'student_teachers',
            recipient_id=student_id,
            payload={
                'studentId': student_id,
                'studentName': student_name,
                'date': date,
                'status': 'read',
            },
            idempotency_key=f'contact_book_status:{student_id}:{date}:read:{read_at}',
        )
        conn.commit()
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# PUT /<student_id>/<date>/sign
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/<student_id>/<date>/sign', methods=['PUT'])
def mark_as_signed(student_id, date):
    """Parent signs the contact book.
    Auto-creates a contact_books row if none exists (sparse-table)."""
    data = request.get_json() or {}
    conn = data_service.get_db()
    try:
        _ensure_contact_book_row(conn, student_id, date)
        row = conn.execute(
            'SELECT original_parent FROM contact_books WHERE student_id = ? AND date = ?',
            (student_id, date),
        ).fetchone()

        signed_at = data.get('signedAt') or datetime.now().isoformat()
        signature_url = data.get('signatureUrl')

        if data.get('note'):
            parent_obj = load_json(row['original_parent']) or {}
            parent_obj['note'] = data['note']
            parent_obj['updatedAt'] = signed_at
            conn.execute(
                'UPDATE contact_books SET signed_at = ?, original_parent = ?, '
                'parent_signature_url = ?, last_modified = ? '
                'WHERE student_id = ? AND date = ?',
                (signed_at, json.dumps(parent_obj, ensure_ascii=False),
                 signature_url, datetime.now().isoformat(), student_id, date),
            )
        else:
            conn.execute(
                'UPDATE contact_books SET signed_at = ?, parent_signature_url = ?, '
                'last_modified = ? WHERE student_id = ? AND date = ?',
                (signed_at, signature_url, datetime.now().isoformat(), student_id, date),
            )
        student_name = data.get('studentName', student_id)
        enqueue_push_job(
            conn,
            EVENT_CONTACT_BOOK_TEACHER_STATUS,
            'student_teachers',
            recipient_id=student_id,
            payload={
                'studentId': student_id,
                'studentName': student_name,
                'date': date,
                'status': 'signed',
            },
            idempotency_key=f'contact_book_status:{student_id}:{date}:signed:{signed_at}',
        )
        conn.commit()
        return jsonify({'status': 'signed'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# PUT /<student_id>/<date>/items-checked
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/<student_id>/<date>/items-checked', methods=['PUT'])
def update_items_checked(student_id, date):
    data = request.get_json() or {}
    conn = data_service.get_db()
    try:
        row = conn.execute(
            'SELECT items_to_bring FROM contact_books WHERE student_id = ? AND date = ?',
            (student_id, date),
        ).fetchone()
        if not row:
            return jsonify({'error': 'Record not found'}), 404
        items_obj = load_json(row['items_to_bring']) or {}
        items_obj['checkedItems'] = data.get('checkedItems', [])
        items_obj['checkedAt'] = data.get('checkedAt') or datetime.now().isoformat()
        conn.execute(
            'UPDATE contact_books SET items_to_bring = ?, last_modified = ? '
            'WHERE student_id = ? AND date = ?',
            (json.dumps(items_obj, ensure_ascii=False), datetime.now().isoformat(),
             student_id, date),
        )
        conn.commit()
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# Comments
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/<student_id>/<date>/comments', methods=['GET', 'POST'])
def handle_comments(student_id, date):
    conn = data_service.get_db()
    try:
        if request.method == 'POST':
            data = request.get_json() or {}
            if not data.get('content'):
                return jsonify({'error': 'Content is required'}), 400

            # Posting a comment may auto-create the row (parents can comment
            # even if no teacher data exists yet — sparse table)
            _ensure_contact_book_row(conn, student_id, date)

        row = conn.execute(
            'SELECT comments FROM contact_books WHERE student_id = ? AND date = ?',
            (student_id, date),
        ).fetchone()
        if not row and request.method == 'GET':
            return jsonify([]), 200
        comments = load_json(row['comments']) if row else []
        comments = comments or []

        if request.method == 'GET':
            profiles = _load_teacher_profiles(conn)
            return jsonify(_enrich_comments(comments, profiles)), 200

        # POST
        data = request.get_json() or {}
        now_iso = datetime.now().isoformat()
        sender_role = data.get('senderRole', 'parent')
        sender_id = data.get('senderId', 'parent')
        comment = {
            'id': f"{student_id}_{date}_{datetime.now().timestamp():.6f}",
            'senderId': sender_id,
            'senderRole': sender_role,
            'content': data['content'],
            'createdAt': now_iso,
        }
        if sender_role == 'parent':
            comment['name'] = data.get('name', '家長')
            if data.get('userId'):
                comment['userId'] = data['userId']
                threading.Thread(
                    target=_upsert_parent_profile,
                    args=(data['userId'], data.get('name', '家長'), data.get('pictureUrl', '')),
                    daemon=True,
                ).start()
        comments.append(comment)
        conn.execute(
            'UPDATE contact_books SET comments = ?, last_modified = ? '
            'WHERE student_id = ? AND date = ?',
            (json.dumps(comments, ensure_ascii=False), now_iso, student_id, date),
        )

        profiles = _load_teacher_profiles(conn)
        if sender_role in ('teacher', 'admin'):
            tp = profiles.get(sender_id, {})
            sender_display = tp.get('ename') or tp.get('cname') or '老師'
            comment['cname'] = tp.get('cname', '')
            comment['ename'] = tp.get('ename', '')
        else:
            sender_display = data.get('name', '家長')

        content_preview = comment['content']
        student_name = data.get('studentName') or student_id
        if sender_role in ('teacher', 'admin'):
            enqueue_push_job(
                conn,
                EVENT_CONTACT_BOOK_PARENT_COMMENT,
                'student_parents',
                recipient_id=student_id,
                payload={
                    'studentId': student_id,
                    'studentName': student_name,
                    'senderName': sender_display,
                    'content': content_preview,
                    'date': date,
                },
                idempotency_key=f'contact_book_comment_parent:{comment["id"]}',
                pref_column='contact_book_notify',
            )
        else:
            enqueue_push_job(
                conn,
                EVENT_CONTACT_BOOK_TEACHER_COMMENT,
                'student_teachers',
                recipient_id=student_id,
                payload={
                    'studentId': student_id,
                    'studentName': student_name,
                    'senderName': sender_display,
                    'content': content_preview,
                    'date': date,
                },
                idempotency_key=f'contact_book_comment_teacher:{comment["id"]}',
            )
        conn.commit()
        return jsonify(comment), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@contact_book_bp.route('/<student_id>/<date>/comments/<comment_id>', methods=['PATCH'])
def edit_comment(student_id, date, comment_id):
    comment_id = unquote(comment_id)
    body = request.get_json() or {}
    if 'content' not in body:
        return jsonify({'error': 'Missing content'}), 400
    new_content = body['content'].strip()
    if not new_content:
        return jsonify({'error': 'Content cannot be empty'}), 400

    conn = data_service.get_db()
    try:
        row = conn.execute(
            'SELECT comments FROM contact_books WHERE student_id = ? AND date = ?',
            (student_id, date),
        ).fetchone()
        if not row:
            return jsonify({'error': 'Record not found'}), 404
        comments = load_json(row['comments']) or []

        def _matches(c):
            return (
                c.get('id') == comment_id
                or c.get('content') == comment_id
                or c.get('createdAt') == comment_id
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
            'UPDATE contact_books SET comments = ?, last_modified = ? '
            'WHERE student_id = ? AND date = ?',
            (json.dumps(updated_comments, ensure_ascii=False),
             datetime.now().isoformat(), student_id, date),
        )
        conn.commit()
        return jsonify(updated_comment), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@contact_book_bp.route('/<student_id>/<date>/comments/<comment_id>', methods=['DELETE'])
def delete_comment(student_id, date, comment_id):
    comment_id = unquote(comment_id)
    conn = data_service.get_db()
    try:
        row = conn.execute(
            'SELECT comments FROM contact_books WHERE student_id = ? AND date = ?',
            (student_id, date),
        ).fetchone()
        if not row:
            return jsonify({'error': 'Record not found'}), 404
        comments = load_json(row['comments']) or []
        original_len = len(comments)

        def _matches(c):
            return (
                c.get('id') == comment_id
                or c.get('content') == comment_id
                or c.get('createdAt') == comment_id
            )

        deleted = [c for c in comments if _matches(c)]
        filtered = [c for c in comments if not _matches(c)]
        if len(filtered) == original_len:
            return jsonify({'error': 'Comment not found'}), 404

        deleted_content = deleted[0].get('content', '') if deleted else ''
        deleted_sender_name = deleted[0].get('name', '家長') if deleted else '家長'
        deleted_sender_role = deleted[0].get('senderRole', 'parent') if deleted else 'parent'
        is_deleted_image = (
            isinstance(deleted_content, str) and (
                deleted_content.startswith('https://firebasestorage.googleapis.com')
                or deleted_content.startswith('https://storage.googleapis.com')
                or deleted_content.startswith('https://imageserver.wentzao.com')
            )
        )
        deleted_image_url = deleted_content if is_deleted_image else ''

        conn.execute(
            'UPDATE contact_books SET comments = ?, last_modified = ? '
            'WHERE student_id = ? AND date = ?',
            (json.dumps(filtered, ensure_ascii=False), datetime.now().isoformat(),
             student_id, date),
        )
        if deleted_sender_role == 'teacher':
            enqueue_push_job(
                conn,
                EVENT_CONTACT_BOOK_PARENT_COMMENT_DELETED,
                'student_parents',
                recipient_id=student_id,
                payload={
                    'studentId': student_id,
                    'date': date,
                    'imageUrl': deleted_image_url,
                },
                idempotency_key=f'contact_book_comment_deleted_parent:{student_id}:{date}:{comment_id}',
                pref_column='contact_book_notify',
            )
        else:
            enqueue_push_job(
                conn,
                EVENT_CONTACT_BOOK_TEACHER_COMMENT_DELETED,
                'student_teachers',
                recipient_id=student_id,
                payload={
                    'studentId': student_id,
                    'date': date,
                    'content': deleted_content,
                    'senderName': deleted_sender_name,
                },
                idempotency_key=f'contact_book_comment_deleted_teacher:{student_id}:{date}:{comment_id}',
            )
        conn.commit()
        return jsonify({'status': 'deleted', 'remaining': len(filtered)}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# Batch teacher save (journal editor auto-save)
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/batch/<class_name>/<date>/teacher', methods=['PUT'])
def batch_save_teacher(class_name, date):
    """Batch save teacher entries for multiple students. No status mutation —
    visibility is owned by grants."""
    body = request.get_json() or {}
    if 'students' not in body:
        return jsonify({'error': 'Missing students data'}), 400

    students_data = body['students']
    edited_by_raw = body.get('editedBy')
    last_known_modified = body.get('lastModified', {})
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
        for student_id, raw_note_data in students_data.items():
            note_data = normalize_teacher_payload(dict(raw_note_data or {}))
            known = last_known_modified.get(student_id)
            if known:
                row_check = conn.execute(
                    'SELECT last_modified, edited_by FROM contact_books WHERE student_id = ? AND date = ?',
                    (student_id, date),
                ).fetchone()
                if row_check and row_check['last_modified'] and row_check['last_modified'] != known:
                    conflicts[student_id] = {
                        'serverModified': row_check['last_modified'],
                        'editedBy': json.loads(row_check['edited_by']) if row_check['edited_by'] else None,
                    }
                    continue

            raw_items = note_data.pop('itemsToBring', None)
            items_to_bring = (
                json.dumps({'items': raw_items}, ensure_ascii=False)
                if isinstance(raw_items, list) and raw_items else None
            )
            raw_returned = note_data.pop('returnedItems', None)
            returned_items = (
                json.dumps(raw_returned, ensure_ascii=False)
                if isinstance(raw_returned, list) and raw_returned else None
            )
            survey_id = note_data.pop('surveyId', None) or None
            teacher_json = json.dumps(note_data, ensure_ascii=False)

            row = conn.execute(
                '''SELECT id, original_teacher, items_to_bring, returned_items, survey_id
                   FROM contact_books WHERE student_id = ? AND date = ?''',
                (student_id, date),
            ).fetchone()

            now = datetime.now().isoformat()
            if not row:
                conn.execute(
                    '''
                    INSERT INTO contact_books (
                        student_id, date, year, month, original_teacher,
                        items_to_bring, returned_items, survey_id, edited_by, last_modified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (student_id, date, year, month, teacher_json,
                     items_to_bring, returned_items, survey_id, edited_by, now),
                )
            else:
                merged, preserved = merge_existing_visible_content_if_empty(
                    note_data,
                    load_json(row['original_teacher']) or {},
                    items_to_bring=raw_items,
                    returned_items=raw_returned,
                    survey_id=survey_id,
                )
                if preserved:
                    if items_to_bring is None:
                        items_to_bring = row['items_to_bring']
                    if returned_items is None:
                        returned_items = row['returned_items']
                    if survey_id is None:
                        survey_id = row['survey_id']
                teacher_json = json.dumps(merged, ensure_ascii=False)
                conn.execute(
                    '''UPDATE contact_books SET original_teacher = ?, items_to_bring = ?,
                       returned_items = ?, survey_id = ?, edited_by = ?, last_modified = ?
                       WHERE student_id = ? AND date = ?''',
                    (teacher_json, items_to_bring, returned_items, survey_id,
                     edited_by, now, student_id, date),
                )
            saved_count += 1

        if saved_count > 0:
            saved_ids = [sid for sid in students_data.keys() if sid not in conflicts]
            rows = conn.execute(
                'SELECT DISTINCT user_id FROM teacher_class_memberships WHERE class_name = ?',
                (class_name,),
            ).fetchall()
            enqueue_push_job(
                conn,
                EVENT_TEACHER_USER_IDS_PUSH,
                'teacher_user_ids',
                recipient_id=class_name,
                payload={
                    'userIds': [r['user_id'] for r in rows],
                    'title': '',
                    'body': '',
                    'data': {
                        'type': 'data_updated',
                        'dataType': 'student_notes',
                        'className': class_name,
                        'date': date,
                        'studentIds': json.dumps(saved_ids),
                    },
                },
                idempotency_key=f'contact_book_batch_data_updated:{class_name}:{date}:{datetime.now().isoformat()}',
            )

        conn.commit()
        result = {'status': 'saved', 'count': saved_count}
        if conflicts:
            result['conflicts'] = conflicts
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# Admin: clear a date
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/admin/clear-date/<date>', methods=['DELETE'])
def admin_clear_date(date):
    conn = data_service.get_db()
    try:
        cb = conn.execute('DELETE FROM contact_books WHERE date = ?', (date,)).rowcount
        cj = conn.execute('DELETE FROM class_journals WHERE date = ?', (date,)).rowcount
        cg = conn.execute('DELETE FROM class_notification_grants WHERE date = ?', (date,)).rowcount
        sn = conn.execute('DELETE FROM scheduled_class_notifications WHERE date = ?', (date,)).rowcount
        conn.commit()
        return jsonify({'deleted': {
            'contactBooks': cb,
            'classJournals': cj,
            'classNotificationGrants': cg,
            'scheduledClassNotifications': sn,
        }}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# GET /class/<class_name>/<date>/teacher
# ──────────────────────────────────────────────────────────

@contact_book_bp.route('/class/<class_name>/<date>/teacher', methods=['GET'])
def get_class_date_teacher(class_name, date):
    """Teacher journal editor: returns flat map { studentId: noteData }.
    Each noteData includes derived status (from per-student read/sign and
    whether the grant covers them) and the (class-level) scheduled notification."""
    conn = data_service.get_db()
    try:
        rows = conn.execute(
            '''SELECT student_id, original_teacher, items_to_bring, returned_items,
                      survey_id, read_at, signed_at
               FROM contact_books WHERE date = ?''',
            (date,),
        ).fetchall()

        grant = grant_service.get_grant(conn, class_name, date)
        grant_dict = grant_service.serialize_grant(grant)
        grant_student_ids = set(grant_dict['studentIds']) if grant_dict and not grant_dict.get('cancelledAt') else set()

        sched = conn.execute(
            '''SELECT * FROM scheduled_class_notifications
               WHERE class_name = ? AND date = ? AND status = ?
               ORDER BY send_at DESC, id DESC LIMIT 1''',
            (class_name, date, grant_service.SCHEDULE_PENDING),
        ).fetchone()
        sched_dict = grant_service.serialize_scheduled_notification(sched)

        result = {}
        for r in rows:
            teacher_data = load_json(r['original_teacher']) or {}
            sid = r['student_id']
            teacher_data['status'] = _derived_status(
                r['read_at'], r['signed_at'], sid in grant_student_ids,
            )
            teacher_data['readAt'] = r['read_at']
            teacher_data['signedAt'] = r['signed_at']
            items = load_json(r['items_to_bring'])
            if items and 'items' in items:
                teacher_data['itemsToBring'] = items['items']
            returned = load_json(r['returned_items'])
            if returned:
                teacher_data['returnedItems'] = returned
            if r['survey_id']:
                teacher_data['surveyId'] = r['survey_id']
            teacher_data['scheduledNotification'] = sched_dict
            result[sid] = teacher_data

        return jsonify(result), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

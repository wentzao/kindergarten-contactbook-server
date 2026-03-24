from flask import Blueprint, request, jsonify
from datetime import datetime
from services.data_service import DataService
import json
import os
import traceback

notification_bp = Blueprint('notifications', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)


def ensure_tables():
    """Create notification tables if they don't exist."""
    conn = None
    try:
        conn = data_service.get_db()
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS teacher_profiles (
                user_id VARCHAR(100) PRIMARY KEY,
                cname VARCHAR(100),
                ename VARCHAR(100),
                updated_at VARCHAR(50)
            );

            CREATE TABLE IF NOT EXISTS push_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(100) NOT NULL,
                push_token VARCHAR(200) NOT NULL,
                device_name VARCHAR(100),
                role VARCHAR(20) DEFAULT 'parent',
                created_at VARCHAR(50),
                updated_at VARCHAR(50),
                UNIQUE(user_id, push_token)
            );

            CREATE TABLE IF NOT EXISTS notification_preferences (
                user_id VARCHAR(100) PRIMARY KEY,
                contact_book_notify BOOLEAN DEFAULT 1,
                announcement_notify BOOLEAN DEFAULT 1,
                updated_at VARCHAR(50)
            );

            CREATE TABLE IF NOT EXISTS teacher_comment_reads (
                teacher_id VARCHAR(100) NOT NULL,
                student_id VARCHAR(100) NOT NULL,
                last_read_at VARCHAR(50) NOT NULL,
                PRIMARY KEY (teacher_id, student_id)
            );

            CREATE TABLE IF NOT EXISTS notification_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_name VARCHAR(100) NOT NULL,
                date VARCHAR(20) NOT NULL,
                student_count INTEGER NOT NULL,
                student_ids TEXT,
                sent_by VARCHAR(100),
                sent_at VARCHAR(50) NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_nlog_class_date ON notification_logs(class_name, date);
        ''')
        # Migration: add role column if it doesn't exist
        try:
            conn.execute('ALTER TABLE push_tokens ADD COLUMN role VARCHAR(20) DEFAULT \'parent\'')
            conn.commit()
        except Exception:
            pass  # Column already exists
        # Migration: add student_ids column if it doesn't exist
        try:
            conn.execute('ALTER TABLE push_tokens ADD COLUMN student_ids TEXT')
            conn.commit()
        except Exception:
            pass  # Column already exists
        # Migration: add notified_at column to contact_books for batch notification tracking
        try:
            conn.execute('ALTER TABLE contact_books ADD COLUMN notified_at VARCHAR(50)')
            conn.commit()
        except Exception:
            pass  # Column already exists
        conn.commit()

        # Enable WAL mode once at startup (safe here — no concurrent connections yet)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
        except Exception:
            pass
    finally:
        if conn:
            conn.close()


# Run on import to ensure tables
ensure_tables()


# ==========================================
# Push Token API
# ==========================================

@notification_bp.route('/push-token', methods=['POST'])
def register_push_token():
    """Register a FCM push token. Teachers: 1 token only. Parents: multi-device."""
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    user_id = data.get('userId')
    push_token = data.get('pushToken')

    if not user_id or not push_token:
        return jsonify({'error': 'userId and pushToken are required'}), 400

    device_name = data.get('deviceName', 'Unknown')
    role = data.get('role', 'parent')
    student_ids = json.dumps(data.get('studentIds', []), ensure_ascii=False) if data.get('studentIds') else None
    now = datetime.now().isoformat()

    conn = data_service.get_db()
    try:
        if role in ('teacher', 'admin'):
            # Teachers/admins: only keep the LATEST token (1 device per user per role)
            conn.execute('DELETE FROM push_tokens WHERE user_id = ? AND role = ?', (user_id, role))
            conn.execute('''
                INSERT INTO push_tokens (user_id, push_token, device_name, role, student_ids, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, push_token, device_name, role, student_ids, now, now))
        else:
            # Parents: allow multiple devices (UPSERT by user_id + push_token)
            conn.execute('''
                INSERT INTO push_tokens (user_id, push_token, device_name, role, student_ids, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, push_token) DO UPDATE SET
                    device_name = excluded.device_name,
                    role = excluded.role,
                    student_ids = excluded.student_ids,
                    updated_at = excluded.updated_at
            ''', (user_id, push_token, device_name, role, student_ids, now, now))
        conn.commit()
        return jsonify({'status': 'ok', 'message': 'Push token registered'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@notification_bp.route('/push-token/clear-all', methods=['POST', 'DELETE'])
def clear_all_tokens():
    """Clear all push tokens (admin use)."""
    conn = data_service.get_db()
    try:
        conn.execute('DELETE FROM push_tokens')
        conn.commit()
        return jsonify({'status': 'ok', 'message': 'All push tokens cleared'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@notification_bp.route('/push-token', methods=['DELETE'])
def remove_push_token():
    """Remove a FCM push token (on user logout)."""
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    user_id = data.get('userId')
    push_token = data.get('pushToken')

    if not user_id or not push_token:
        return jsonify({'error': 'userId and pushToken are required'}), 400

    conn = data_service.get_db()
    try:
        conn.execute(
            'DELETE FROM push_tokens WHERE user_id = ? AND push_token = ?',
            (user_id, push_token)
        )
        conn.commit()
        return jsonify({'status': 'ok', 'message': 'Push token removed'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ==========================================
# Notification Preferences API
# ==========================================

@notification_bp.route('/preferences', methods=['GET'])
def get_preferences():
    """Get notification preferences for a user."""
    user_id = request.args.get('userId')
    if not user_id:
        return jsonify({'error': 'userId parameter is required'}), 400

    conn = data_service.get_db()
    try:
        row = conn.execute(
            'SELECT * FROM notification_preferences WHERE user_id = ?',
            (user_id,)
        ).fetchone()

        if row:
            return jsonify({
                'userId': row['user_id'],
                'contactBookNotify': bool(row['contact_book_notify']),
                'announcementNotify': bool(row['announcement_notify']),
            })
        else:
            # Return defaults if no preferences set yet
            return jsonify({
                'userId': user_id,
                'contactBookNotify': True,
                'announcementNotify': True,
            })
    finally:
        conn.close()


@notification_bp.route('/preferences', methods=['PUT'])
def update_preferences():
    """Update notification preferences for a user."""
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    user_id = data.get('userId')
    if not user_id:
        return jsonify({'error': 'userId is required'}), 400

    contact_book = 1 if data.get('contactBookNotify', True) else 0
    announcement = 1 if data.get('announcementNotify', True) else 0
    now = datetime.now().isoformat()

    conn = data_service.get_db()
    try:
        conn.execute('''
            INSERT INTO notification_preferences (user_id, contact_book_notify, announcement_notify, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                contact_book_notify = excluded.contact_book_notify,
                announcement_notify = excluded.announcement_notify,
                updated_at = excluded.updated_at
        ''', (user_id, contact_book, announcement, now))
        conn.commit()

        return jsonify({
            'status': 'ok',
            'userId': user_id,
            'contactBookNotify': bool(contact_book),
            'announcementNotify': bool(announcement),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ==========================================
# Unread Comment Counts
# ==========================================

@notification_bp.route('/unread-comments/<teacher_id>', methods=['GET'])
def get_unread_comments(teacher_id):
    """
    Get unread parent comment counts per student + unread dates.
    Returns: { "counts": { "studentId": count }, "dates": { "studentId": ["2026-03-03", ...] } }
    """
    import json
    conn = data_service.get_db()
    try:
        # Get all contact book entries that have comments
        rows = conn.execute(
            "SELECT student_id, date, comments FROM contact_books WHERE comments IS NOT NULL AND comments != '[]'"
        ).fetchall()

        # Get teacher's last-read timestamps
        reads = conn.execute(
            'SELECT student_id, last_read_at FROM teacher_comment_reads WHERE teacher_id = ?',
            (teacher_id,)
        ).fetchall()
        last_read_map = {r['student_id']: r['last_read_at'] for r in reads}

        # Count unread comments per student + collect dates
        unread_counts = {}
        unread_dates = {}
        for row in rows:
            student_id = row['student_id']
            record_date = row['date']
            try:
                comments = json.loads(row['comments']) if row['comments'] else []
            except:
                continue

            last_read = last_read_map.get(student_id, '1970-01-01')

            # Count parent comments newer than last_read
            unread = sum(
                1 for c in comments
                if c.get('senderRole', '') == 'parent' or c.get('senderId') == 'parent'
                if c.get('createdAt', '') > last_read
            )

            if unread > 0:
                unread_counts[student_id] = unread_counts.get(student_id, 0) + unread
                if student_id not in unread_dates:
                    unread_dates[student_id] = []
                unread_dates[student_id].append(record_date)

        return jsonify({'counts': unread_counts, 'dates': unread_dates}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@notification_bp.route('/mark-comments-read', methods=['POST'])
def mark_comments_read():
    """Mark comments as read for a teacher + student."""
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    teacher_id = data.get('teacherId')
    student_id = data.get('studentId')
    if not teacher_id or not student_id:
        return jsonify({'error': 'teacherId and studentId required'}), 400

    now = datetime.now().isoformat()
    conn = data_service.get_db()
    try:
        conn.execute('''
            INSERT INTO teacher_comment_reads (teacher_id, student_id, last_read_at)
            VALUES (?, ?, ?)
            ON CONFLICT(teacher_id, student_id) DO UPDATE SET
                last_read_at = excluded.last_read_at
        ''', (teacher_id, student_id, now))
        conn.commit()
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ==========================================
# Health Check
# ==========================================

@notification_bp.route('/health', methods=['GET'])
def health_check():
    """Quick DB health check — useful for diagnosing 500 errors."""
    conn = data_service.get_db()
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(contact_books)').fetchall()]
        row_count = conn.execute('SELECT COUNT(*) as cnt FROM contact_books').fetchone()['cnt']
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        return jsonify({
            'status': 'ok',
            'contactBooksColumns': cols,
            'contactBooksRows': row_count,
            'integrityCheck': integrity,
        }), 200
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e), 'trace': traceback.format_exc()}), 500
    finally:
        conn.close()


# ==========================================
# Batch Notification API
# ==========================================

@notification_bp.route('/pending', methods=['POST'])
def get_pending_notifications():
    """Get contact book entries that are completed but parents have not been notified yet."""
    data = request.json or {}
    student_ids = data.get('studentIds', [])
    date_filter = data.get('date')

    if not student_ids or not isinstance(student_ids, list):
        return jsonify({'error': 'studentIds must be a non-empty array'}), 400

    # Filter out any null/None values
    student_ids = [sid for sid in student_ids if sid]
    if not student_ids:
        return jsonify({'count': 0, 'entries': [], 'notifiedCount': 0}), 200

    conn = None
    try:
        conn = data_service.get_db()
        placeholders = ','.join('?' for _ in student_ids)

        # Draft entries (teacher saved but not yet notified)
        query = f'''
            SELECT student_id, date, status FROM contact_books
            WHERE student_id IN ({placeholders})
            AND status = 'draft'
        '''
        params = list(student_ids)
        if date_filter:
            query += ' AND date = ?'
            params.append(date_filter)
        rows = conn.execute(query, params).fetchall()
        entries = [{'studentId': r['student_id'], 'date': r['date'], 'status': r['status']} for r in rows]

        # Already-notified student IDs (status in notified/read/signed)
        notified_query = f'''
            SELECT student_id FROM contact_books
            WHERE student_id IN ({placeholders})
            AND status IN ('notified', 'read', 'signed')
        '''
        notified_params = list(student_ids)
        if date_filter:
            notified_query += ' AND date = ?'
            notified_params.append(date_filter)
        notified_rows = conn.execute(notified_query, notified_params).fetchall()
        notified_student_ids = [r['student_id'] for r in notified_rows]
        notified_count = len(notified_student_ids)

        return jsonify({
            'count': len(entries),
            'entries': entries,
            'notifiedCount': notified_count,
            'notifiedStudentIds': notified_student_ids,
        }), 200
    except Exception as e:
        print(f'[Pending] ERROR for studentIds={student_ids[:3]}... date={date_filter}: {e}\n{traceback.format_exc()}')
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@notification_bp.route('/send-batch', methods=['POST'])
def send_batch_notifications():
    """Send contact book notifications in batch for completed but un-notified entries."""
    import threading

    data = request.json or {}
    student_ids = data.get('studentIds', [])
    date_filter = data.get('date')

    if not student_ids:
        return jsonify({'error': 'studentIds is required'}), 400

    conn = None
    try:
        conn = data_service.get_db()
        placeholders = ','.join('?' for _ in student_ids)

        # Find draft entries to promote to notified
        query = f'''
            SELECT student_id, date FROM contact_books
            WHERE student_id IN ({placeholders})
            AND status = 'draft'
        '''
        params = list(student_ids)
        if date_filter:
            query += ' AND date = ?'
            params.append(date_filter)

        rows = conn.execute(query, params).fetchall()
        if not rows:
            return jsonify({'sent': 0, 'total': 0, 'message': 'No draft entries to notify'}), 200

        now = datetime.now().isoformat()
        sent_count = 0

        try:
            from services.send_notification import notify_parents_new_record
        except Exception:
            return jsonify({'error': 'Notification service unavailable'}), 500

        for row in rows:
            sid = row['student_id']
            d = row['date']
            student_name = data.get('studentNames', {}).get(sid, sid)

            def _send(s_id=sid, s_name=student_name, s_date=d):
                try:
                    notify_parents_new_record(data_service, s_id, s_name, s_date)
                except Exception as e:
                    print(f'[Notification] Batch send error for {s_id}: {e}')
            threading.Thread(target=_send, daemon=True).start()

            # draft → notified
            conn.execute(
                'UPDATE contact_books SET status = ?, notified_at = ? WHERE student_id = ? AND date = ?',
                ('notified', now, sid, d)
            )
            sent_count += 1

        conn.commit()
        return jsonify({'sent': sent_count, 'total': len(rows)}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()



# ==========================================
# Pre-send Checklist
# ==========================================

@notification_bp.route('/checklist/<class_name>/<date>', methods=['POST'])
def get_checklist(class_name, date):
    """Return completion status for each student in the class for a given date.
    Body: { studentIds: [...], studentNames: { id: name } }
    Returns: { journal: { exists, blockCount }, students: [ { id, name, hasNotes, hasHealth, status } ] }
    """
    data = request.json or {}
    student_ids = data.get('studentIds', [])
    student_names = data.get('studentNames', {})

    conn = data_service.get_db()
    try:
        # Check class journal
        journal = conn.execute(
            'SELECT content_blocks, notified_at FROM class_journals WHERE class_name = ? AND date = ?',
            (class_name, date)
        ).fetchone()
        journal_blocks = json.loads(journal['content_blocks']) if journal and journal['content_blocks'] else []
        journal_info = {
            'exists': len(journal_blocks) > 0,
            'blockCount': len(journal_blocks),
            'alreadyNotified': bool(journal['notified_at']) if journal else False,
        }

        # Check each student's contact book
        students = []
        for sid in student_ids:
            row = conn.execute(
                'SELECT original_teacher, status, notified_at FROM contact_books WHERE student_id = ? AND date = ?',
                (sid, date)
            ).fetchone()
            teacher_data = json.loads(row['original_teacher']) if row and row['original_teacher'] else None
            has_notes = False
            has_health = False
            if teacher_data:
                blocks = teacher_data.get('blocks', [])
                note = teacher_data.get('note', '')
                has_notes = len(blocks) > 0 or bool(note and note.strip())
                has_health = bool(teacher_data.get('health') or teacher_data.get('mood'))

            students.append({
                'id': sid,
                'name': student_names.get(sid, sid),
                'hasNotes': has_notes,
                'hasHealth': has_health,
                'status': row['status'] if row else None,
                'notifiedAt': row['notified_at'] if row else None,
            })

        return jsonify({
            'journal': journal_info,
            'students': students,
            'filledCount': sum(1 for s in students if s['hasNotes'] or s['hasHealth']),
            'totalCount': len(students),
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ==========================================
# Notification Logs
# ==========================================

@notification_bp.route('/logs/<date>', methods=['GET'])
def get_notification_logs(date):
    """Get notification send logs for a given date, grouped by class."""
    conn = None
    try:
        conn = data_service.get_db()
        rows = conn.execute(
            'SELECT * FROM notification_logs WHERE date = ? ORDER BY sent_at DESC',
            (date,)
        ).fetchall()

        logs = {}
        for r in rows:
            cn = r['class_name']
            if cn not in logs:
                logs[cn] = []
            logs[cn].append({
                'id': r['id'],
                'studentCount': r['student_count'],
                'studentIds': json.loads(r['student_ids']) if r['student_ids'] else [],
                'sentBy': r['sent_by'],
                'sentAt': r['sent_at'],
            })

        return jsonify({'date': date, 'logs': logs}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


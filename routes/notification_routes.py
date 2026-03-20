from flask import Blueprint, request, jsonify
from datetime import datetime
from services.data_service import DataService
import json
import os

notification_bp = Blueprint('notifications', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)


def ensure_tables():
    """Create notification tables if they don't exist."""
    conn = data_service.get_db()
    try:
        conn.executescript('''
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
        # Create notification_schedules table for per-class scheduled notifications
        conn.execute('''
            CREATE TABLE IF NOT EXISTS notification_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_name VARCHAR(50) NOT NULL UNIQUE,
                send_time VARCHAR(10) NOT NULL,
                teacher_id VARCHAR(100),
                student_ids TEXT,
                is_active BOOLEAN DEFAULT 1,
                created_at VARCHAR(50),
                updated_at VARCHAR(50)
            )
        ''')
        conn.commit()
    finally:
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
                if c.get('senderId', 'parent') == 'parent'
                and c.get('createdAt', '') > last_read
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
# Batch Notification API
# ==========================================

@notification_bp.route('/pending', methods=['POST'])
def get_pending_notifications():
    """Get contact book entries that are completed but parents have not been notified yet."""
    data = request.json or {}
    student_ids = data.get('studentIds', [])
    date_filter = data.get('date')

    if not student_ids:
        return jsonify({'error': 'studentIds is required'}), 400

    conn = data_service.get_db()
    try:
        placeholders = ','.join('?' for _ in student_ids)
        query = f'''
            SELECT student_id, date, status FROM contact_books
            WHERE student_id IN ({placeholders})
            AND status = 'completed'
            AND notified_at IS NULL
        '''
        params = list(student_ids)
        if date_filter:
            query += ' AND date = ?'
            params.append(date_filter)

        rows = conn.execute(query, params).fetchall()
        entries = [{'studentId': r['student_id'], 'date': r['date'], 'status': r['status']} for r in rows]
        return jsonify({'count': len(entries), 'entries': entries}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
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

    conn = data_service.get_db()
    try:
        placeholders = ','.join('?' for _ in student_ids)
        query = f'''
            SELECT student_id, date FROM contact_books
            WHERE student_id IN ({placeholders})
            AND status = 'completed'
            AND notified_at IS NULL
        '''
        params = list(student_ids)
        if date_filter:
            query += ' AND date = ?'
            params.append(date_filter)

        rows = conn.execute(query, params).fetchall()
        if not rows:
            return jsonify({'sent': 0, 'total': 0, 'message': 'No pending notifications'}), 200

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

            conn.execute(
                'UPDATE contact_books SET notified_at = ? WHERE student_id = ? AND date = ?',
                (now, sid, d)
            )
            sent_count += 1

        conn.commit()
        return jsonify({'sent': sent_count, 'total': len(rows)}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ==========================================
# Notification Schedule API
# ==========================================

@notification_bp.route('/schedule', methods=['GET'])
def get_schedule():
    """Get notification schedule for a class."""
    class_name = request.args.get('className')
    conn = data_service.get_db()
    try:
        if class_name:
            row = conn.execute(
                'SELECT * FROM notification_schedules WHERE class_name = ?',
                (class_name,)
            ).fetchone()
            if row:
                return jsonify({
                    'className': row['class_name'],
                    'sendTime': row['send_time'],
                    'teacherId': row['teacher_id'],
                    'studentIds': json.loads(row['student_ids']) if row['student_ids'] else [],
                    'isActive': bool(row['is_active']),
                })
            else:
                return jsonify({'className': class_name, 'sendTime': None, 'isActive': False})
        else:
            rows = conn.execute('SELECT * FROM notification_schedules').fetchall()
            schedules = [{
                'className': r['class_name'],
                'sendTime': r['send_time'],
                'teacherId': r['teacher_id'],
                'studentIds': json.loads(r['student_ids']) if r['student_ids'] else [],
                'isActive': bool(r['is_active']),
            } for r in rows]
            return jsonify({'schedules': schedules}), 200
    finally:
        conn.close()


@notification_bp.route('/schedule', methods=['POST'])
def set_schedule():
    """Create or update a notification schedule for a class."""
    data = request.json or {}
    class_name = data.get('className')
    send_time = data.get('sendTime')
    teacher_id = data.get('teacherId', '')
    student_ids = json.dumps(data.get('studentIds', []), ensure_ascii=False)
    is_active = 1 if data.get('isActive', True) else 0

    if not class_name or not send_time:
        return jsonify({'error': 'className and sendTime are required'}), 400

    now = datetime.now().isoformat()
    conn = data_service.get_db()
    try:
        conn.execute('''
            INSERT INTO notification_schedules (class_name, send_time, teacher_id, student_ids, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(class_name) DO UPDATE SET
                send_time = excluded.send_time,
                teacher_id = excluded.teacher_id,
                student_ids = excluded.student_ids,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at
        ''', (class_name, send_time, teacher_id, student_ids, is_active, now, now))
        conn.commit()

        _register_schedule_job(class_name, send_time, is_active)

        return jsonify({
            'status': 'ok',
            'className': class_name,
            'sendTime': send_time,
            'isActive': bool(is_active),
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@notification_bp.route('/schedule', methods=['DELETE'])
def delete_schedule():
    """Delete a notification schedule for a class."""
    data = request.json or {}
    class_name = data.get('className')
    if not class_name:
        return jsonify({'error': 'className is required'}), 400

    conn = data_service.get_db()
    try:
        conn.execute('DELETE FROM notification_schedules WHERE class_name = ?', (class_name,))
        conn.commit()
        _remove_schedule_job(class_name)
        return jsonify({'status': 'ok', 'message': f'Schedule for {class_name} deleted'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ==========================================
# Scheduler Helpers
# ==========================================

_scheduler = None


def init_scheduler(scheduler):
    """Called from app.py to inject the APScheduler instance."""
    global _scheduler
    _scheduler = scheduler
    _load_all_schedules()


def _load_all_schedules():
    """Load all active schedules from DB and register APScheduler jobs."""
    conn = data_service.get_db()
    try:
        rows = conn.execute(
            'SELECT class_name, send_time, is_active FROM notification_schedules WHERE is_active = 1'
        ).fetchall()
        for r in rows:
            _register_schedule_job(r['class_name'], r['send_time'], r['is_active'])
        if rows:
            print(f'[Scheduler] Loaded {len(rows)} notification schedule(s)')
    finally:
        conn.close()


def _register_schedule_job(class_name, send_time, is_active):
    """Register or update a cron job in APScheduler."""
    if not _scheduler:
        return
    job_id = f'notify-{class_name}'
    if not is_active:
        _remove_schedule_job(class_name)
        return
    try:
        hour, minute = send_time.split(':')
        _scheduler.add_job(
            func=_run_scheduled_notification,
            trigger='cron',
            hour=int(hour),
            minute=int(minute),
            id=job_id,
            replace_existing=True,
            args=[class_name],
        )
        print(f'[Scheduler] Registered job {job_id} at {send_time}')
    except Exception as e:
        print(f'[Scheduler] Failed to register job {job_id}: {e}')


def _remove_schedule_job(class_name):
    """Remove a cron job from APScheduler."""
    if not _scheduler:
        return
    job_id = f'notify-{class_name}'
    try:
        _scheduler.remove_job(job_id)
        print(f'[Scheduler] Removed job {job_id}')
    except Exception:
        pass  # Job didn't exist


def _run_scheduled_notification(class_name):
    """Executed by APScheduler at the configured time. Sends batch notifications for the class."""
    import threading
    from datetime import date as date_type

    print(f'[Scheduler] Running scheduled notification for {class_name}')
    conn = data_service.get_db()
    try:
        schedule = conn.execute(
            'SELECT student_ids FROM notification_schedules WHERE class_name = ? AND is_active = 1',
            (class_name,)
        ).fetchone()
        if not schedule or not schedule['student_ids']:
            print(f'[Scheduler] No student_ids found for {class_name}')
            return

        student_ids = json.loads(schedule['student_ids'])
        if not student_ids:
            return

        today = date_type.today().isoformat()
        placeholders = ','.join('?' for _ in student_ids)
        rows = conn.execute(f'''
            SELECT student_id, date FROM contact_books
            WHERE student_id IN ({placeholders})
            AND status = 'completed'
            AND notified_at IS NULL
            AND date = ?
        ''', student_ids + [today]).fetchall()

        if not rows:
            print(f'[Scheduler] No pending entries for {class_name} on {today}')
            return

        try:
            from services.send_notification import notify_parents_new_record
        except Exception:
            print('[Scheduler] Notification service unavailable')
            return

        now = datetime.now().isoformat()
        for row in rows:
            sid = row['student_id']
            d = row['date']

            def _send(s_id=sid, s_date=d):
                try:
                    notify_parents_new_record(data_service, s_id, s_id, s_date)
                except Exception as e:
                    print(f'[Scheduler] Send error for {s_id}: {e}')
            threading.Thread(target=_send, daemon=True).start()

            conn.execute(
                'UPDATE contact_books SET notified_at = ? WHERE student_id = ? AND date = ?',
                (now, sid, d)
            )

        conn.commit()
        print(f'[Scheduler] Sent {len(rows)} notifications for {class_name}')
    except Exception as e:
        print(f'[Scheduler] Error for {class_name}: {e}')
    finally:
        conn.close()


from flask import Blueprint, request, jsonify
from datetime import datetime
from services.data_service import DataService
import json
import os
import threading

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
# Contact Book Notification (Manual Trigger)
# ==========================================

@notification_bp.route('/send-contact-book', methods=['POST'])
def send_contact_book_notification():
    """
    Teacher manually triggers contact book notifications for specific students.
    Can send immediately or schedule for a specific time.

    Request body:
    {
        "studentIds": ["B225851150", ...],   // required: list of student IDs
        "date": "2026-03-19",                // required: contact book date
        "scheduleAt": "2026-03-19T16:00:00", // optional: ISO datetime to schedule
                                             // if omitted, sends immediately
    }
    """
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    student_ids = data.get('studentIds', [])
    date = data.get('date')
    schedule_at = data.get('scheduleAt')

    if not student_ids or not date:
        return jsonify({'error': 'studentIds and date are required'}), 400

    # Look up student names from contact_books table
    conn = data_service.get_db()
    try:
        # We don't store student names in contact_books, so we just use student_id
        # The notification function will use student_id as fallback
        pass
    finally:
        conn.close()

    if schedule_at:
        # Schedule for later
        try:
            scheduled_time = datetime.fromisoformat(schedule_at)
            now = datetime.now()
            if scheduled_time <= now:
                return jsonify({'error': 'scheduleAt must be in the future'}), 400

            _schedule_contact_book_push(student_ids, date, scheduled_time)
            return jsonify({
                'status': 'scheduled',
                'scheduledAt': schedule_at,
                'studentCount': len(student_ids),
            }), 200
        except ValueError:
            return jsonify({'error': 'Invalid scheduleAt format. Use ISO 8601.'}), 400
    else:
        # Send immediately in background
        def _send():
            try:
                from services.send_notification import notify_parents_new_record
                total = 0
                for sid in student_ids:
                    total += notify_parents_new_record(data_service, sid, sid, date)
                print(f'[Notification] Sent contact book notification for {len(student_ids)} students, {total} devices reached')
            except Exception as e:
                print(f'[Notification] Error sending contact book notifications: {e}')

        threading.Thread(target=_send, daemon=True).start()
        return jsonify({
            'status': 'sending',
            'studentCount': len(student_ids),
        }), 200


@notification_bp.route('/scheduled-jobs', methods=['GET'])
def get_scheduled_jobs():
    """Get list of pending scheduled notification jobs."""
    jobs = _get_scheduled_jobs()
    return jsonify({'jobs': jobs}), 200


@notification_bp.route('/scheduled-jobs/<job_id>', methods=['DELETE'])
def cancel_scheduled_job(job_id):
    """Cancel a scheduled notification job."""
    success = _cancel_scheduled_job(job_id)
    if success:
        return jsonify({'status': 'cancelled', 'jobId': job_id}), 200
    return jsonify({'error': 'Job not found'}), 404


# ==========================================
# Scheduler helpers (APScheduler)
# ==========================================

_scheduler = None
_scheduler_lock = threading.Lock()


def _get_scheduler():
    """Lazy-init APScheduler. Falls back to threading.Timer if APScheduler is not installed."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    with _scheduler_lock:
        if _scheduler is not None:
            return _scheduler
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            _scheduler = BackgroundScheduler(daemon=True)
            _scheduler.start()
            print('[Scheduler] APScheduler started')
        except ImportError:
            print('[Scheduler] APScheduler not installed, scheduled push will use threading.Timer fallback')
            _scheduler = 'fallback'
    return _scheduler


def _schedule_contact_book_push(student_ids, date, scheduled_time):
    """Schedule a contact book push notification for a future time."""
    scheduler = _get_scheduler()

    def _job():
        try:
            from services.send_notification import notify_parents_new_record
            total = 0
            for sid in student_ids:
                total += notify_parents_new_record(data_service, sid, sid, date)
            print(f'[Scheduler] Executed scheduled push for {len(student_ids)} students, {total} devices')
        except Exception as e:
            print(f'[Scheduler] Error in scheduled job: {e}')

    if scheduler == 'fallback':
        # Fallback: use threading.Timer
        delay = (scheduled_time - datetime.now()).total_seconds()
        if delay > 0:
            t = threading.Timer(delay, _job)
            t.daemon = True
            t.start()
            print(f'[Scheduler] Timer set for {scheduled_time.isoformat()} ({delay:.0f}s)')
    else:
        job_id = f'contact_book_{date}_{datetime.now().timestamp():.0f}'
        scheduler.add_job(
            _job,
            trigger='date',
            run_date=scheduled_time,
            id=job_id,
            replace_existing=True,
        )
        print(f'[Scheduler] Job {job_id} scheduled for {scheduled_time.isoformat()}')


def _get_scheduled_jobs():
    """Return list of pending scheduled jobs."""
    scheduler = _get_scheduler()
    if scheduler == 'fallback':
        return []  # Timer-based fallback doesn't support listing
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            'id': job.id,
            'nextRunTime': job.next_run_time.isoformat() if job.next_run_time else None,
            'name': job.name,
        })
    return jobs


def _cancel_scheduled_job(job_id):
    """Cancel a scheduled job by ID."""
    scheduler = _get_scheduler()
    if scheduler == 'fallback':
        return False
    try:
        scheduler.remove_job(job_id)
        return True
    except Exception:
        return False


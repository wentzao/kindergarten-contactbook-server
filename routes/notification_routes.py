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
        # Migration: recreate notification_schedules with new one-time schema
        # Check if old schema (has 'send_time' column) and migrate
        old_cols = [r[1] for r in conn.execute('PRAGMA table_info(notification_schedules)').fetchall()]
        if old_cols and 'send_time' in old_cols:
            conn.execute('DROP TABLE notification_schedules')
            print('[Migration] Dropped old notification_schedules (recurring schema)')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS notification_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_name VARCHAR(50),
                target_date VARCHAR(20) NOT NULL,
                student_ids TEXT,
                student_names TEXT,
                scheduled_at VARCHAR(50) NOT NULL,
                teacher_id VARCHAR(100),
                teacher_name VARCHAR(100),
                status VARCHAR(20) DEFAULT 'pending',
                created_at VARCHAR(50),
                executed_at VARCHAR(50)
            )
        ''')
        # Fix corrupted index if any
        try:
            conn.execute('REINDEX notification_schedules')
        except Exception:
            pass
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
# Notification Schedule API (one-time schedules)
# ==========================================

def _format_schedule(row):
    """Format a notification_schedules DB row to JSON response."""
    return {
        'id': row['id'],
        'className': row['class_name'],
        'targetDate': row['target_date'],
        'studentIds': json.loads(row['student_ids']) if row['student_ids'] else [],
        'studentNames': json.loads(row['student_names']) if row['student_names'] else {},
        'scheduledAt': row['scheduled_at'],
        'teacherId': row['teacher_id'],
        'teacherName': row['teacher_name'],
        'status': row['status'],
        'createdAt': row['created_at'],
        'executedAt': row['executed_at'],
    }


@notification_bp.route('/schedules', methods=['GET'])
def get_schedules():
    """Get notification schedules. Filters: ?className=, ?targetDate=, ?status=pending"""
    class_name = request.args.get('className')
    target_date = request.args.get('targetDate')
    status = request.args.get('status')

    conn = data_service.get_db()
    try:
        query = 'SELECT * FROM notification_schedules WHERE 1=1'
        params = []
        if class_name:
            query += ' AND class_name = ?'
            params.append(class_name)
        if target_date:
            query += ' AND target_date = ?'
            params.append(target_date)
        if status:
            query += ' AND status = ?'
            params.append(status)
        query += ' ORDER BY scheduled_at ASC'

        rows = conn.execute(query, params).fetchall()
        return jsonify({'schedules': [_format_schedule(r) for r in rows]}), 200
    finally:
        conn.close()


@notification_bp.route('/schedules', methods=['POST'])
def create_schedule():
    """Create a one-time notification schedule.
    Body: { className, targetDate, studentIds, studentNames, scheduledAt, teacherId, teacherName }
    scheduledAt is ISO datetime, e.g. '2026-03-23T16:30:00+08:00'
    """
    data = request.json or {}
    class_name = data.get('className')
    target_date = data.get('targetDate')
    student_ids = data.get('studentIds', [])
    student_names = data.get('studentNames', {})
    scheduled_at = data.get('scheduledAt')
    teacher_id = data.get('teacherId', '')
    teacher_name = data.get('teacherName', '')

    if not target_date or not scheduled_at or not student_ids:
        return jsonify({'error': 'targetDate, scheduledAt, and studentIds are required'}), 400

    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
    conn = data_service.get_db()
    try:
        # Cancel any existing pending schedule for the same class+date
        if class_name:
            conn.execute('''
                UPDATE notification_schedules SET status = 'cancelled'
                WHERE class_name = ? AND target_date = ? AND status = 'pending'
            ''', (class_name, target_date))
        else:
            # Individual student schedule — cancel by same studentIds + date
            conn.execute('''
                UPDATE notification_schedules SET status = 'cancelled'
                WHERE target_date = ? AND student_ids = ? AND status = 'pending'
            ''', (target_date, json.dumps(student_ids, ensure_ascii=False)))

        cursor = conn.execute('''
            INSERT INTO notification_schedules
                (class_name, target_date, student_ids, student_names, scheduled_at,
                 teacher_id, teacher_name, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        ''', (
            class_name, target_date,
            json.dumps(student_ids, ensure_ascii=False),
            json.dumps(student_names, ensure_ascii=False),
            scheduled_at, teacher_id, teacher_name, now
        ))
        conn.commit()
        schedule_id = cursor.lastrowid

        # Register one-time APScheduler job
        _register_onetime_job(schedule_id, scheduled_at)

        return jsonify({
            'status': 'ok',
            'id': schedule_id,
            'scheduledAt': scheduled_at,
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@notification_bp.route('/schedules/<int:schedule_id>', methods=['DELETE'])
def cancel_schedule(schedule_id):
    """Cancel a pending notification schedule."""
    conn = data_service.get_db()
    try:
        row = conn.execute('SELECT * FROM notification_schedules WHERE id = ?', (schedule_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Schedule not found'}), 404
        if row['status'] != 'pending':
            return jsonify({'error': f'Cannot cancel schedule with status: {row["status"]}'}), 400

        conn.execute(
            "UPDATE notification_schedules SET status = 'cancelled' WHERE id = ?",
            (schedule_id,)
        )
        conn.commit()
        _remove_job(schedule_id)
        return jsonify({'status': 'ok', 'message': 'Schedule cancelled'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
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

        # Check if there's a pending schedule
        schedule_row = conn.execute(
            "SELECT * FROM notification_schedules WHERE class_name = ? AND target_date = ? AND status = 'pending'",
            (class_name, date)
        ).fetchone()
        pending_schedule = _format_schedule(schedule_row) if schedule_row else None

        return jsonify({
            'journal': journal_info,
            'students': students,
            'pendingSchedule': pending_schedule,
            'filledCount': sum(1 for s in students if s['hasNotes'] or s['hasHealth']),
            'totalCount': len(students),
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ==========================================
# Scheduler Helpers (one-time jobs)
# ==========================================

_scheduler = None


def init_scheduler(scheduler):
    """Called from app.py to inject the APScheduler instance."""
    global _scheduler
    _scheduler = scheduler
    _load_pending_schedules()


def _load_pending_schedules():
    """On startup: load pending schedules and register APScheduler jobs.
    If scheduled_at has already passed, execute immediately."""
    conn = None
    try:
        conn = data_service.get_db()
        rows = conn.execute(
            "SELECT id, scheduled_at FROM notification_schedules WHERE status = 'pending'"
        ).fetchall()
        now = datetime.now()
        loaded = 0
        for r in rows:
            try:
                # Parse scheduled_at
                sched_str = r['scheduled_at']
                sched_dt = datetime.fromisoformat(sched_str.replace('+08:00', '+0800') if '+08:00' in sched_str else sched_str)
                # Make naive for comparison (assume all times are local Asia/Taipei)
                if sched_dt.tzinfo:
                    sched_dt = sched_dt.replace(tzinfo=None)

                if sched_dt <= now:
                    # Past due — execute immediately
                    print(f'[Scheduler] Schedule {r["id"]} is past due ({sched_str}), executing now')
                    _run_onetime_notification(r['id'])
                else:
                    _register_onetime_job(r['id'], sched_str)
                    loaded += 1
            except Exception as e:
                print(f'[Scheduler] Error loading schedule {r["id"]}: {e}')
        if loaded:
            print(f'[Scheduler] Loaded {loaded} pending notification schedule(s)')
    finally:
        if conn:
            conn.close()


def _register_onetime_job(schedule_id, scheduled_at_str):
    """Register a one-time date-trigger job in APScheduler."""
    if not _scheduler:
        return
    job_id = f'notify-schedule-{schedule_id}'
    try:
        # Parse ISO datetime
        dt = datetime.fromisoformat(scheduled_at_str.replace('+08:00', '+0800') if '+08:00' in scheduled_at_str else scheduled_at_str)
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)

        _scheduler.add_job(
            func=_run_onetime_notification,
            trigger='date',
            run_date=dt,
            id=job_id,
            replace_existing=True,
            args=[schedule_id],
        )
        print(f'[Scheduler] Registered one-time job {job_id} at {scheduled_at_str}')
    except Exception as e:
        print(f'[Scheduler] Failed to register job {job_id}: {e}')


def _remove_job(schedule_id):
    """Remove a job from APScheduler."""
    if not _scheduler:
        return
    job_id = f'notify-schedule-{schedule_id}'
    try:
        _scheduler.remove_job(job_id)
        print(f'[Scheduler] Removed job {job_id}')
    except Exception:
        pass


def _run_onetime_notification(schedule_id):
    """Executed by APScheduler at the scheduled time. Sends notifications and marks schedule as executed."""
    import threading

    print(f'[Scheduler] Running one-time notification for schedule {schedule_id}')
    conn = None
    try:
        conn = data_service.get_db()
        row = conn.execute(
            "SELECT * FROM notification_schedules WHERE id = ? AND status = 'pending'",
            (schedule_id,)
        ).fetchone()
        if not row:
            print(f'[Scheduler] Schedule {schedule_id} not found or already executed/cancelled')
            return

        class_name = row['class_name']
        target_date = row['target_date']
        student_ids = json.loads(row['student_ids']) if row['student_ids'] else []
        student_names = json.loads(row['student_names']) if row['student_names'] else {}

        if not student_ids:
            print(f'[Scheduler] Schedule {schedule_id}: no student_ids')
            return

        # 1. If class-level, publish the class journal
        if class_name:
            conn.execute(
                'UPDATE class_journals SET notified_at = ? WHERE class_name = ? AND date = ? AND notified_at IS NULL',
                (datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00'), class_name, target_date)
            )

        # 2. Update contact_books: draft → notified
        now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
        placeholders = ','.join('?' for _ in student_ids)
        conn.execute(f'''
            UPDATE contact_books SET status = 'notified', notified_at = ?
            WHERE student_id IN ({placeholders}) AND date = ? AND status = 'draft'
        ''', (now, *student_ids, target_date))

        # 3. Mark schedule as executed
        conn.execute(
            "UPDATE notification_schedules SET status = 'executed', executed_at = ? WHERE id = ?",
            (now, schedule_id)
        )
        conn.commit()

        # 4. Send push notifications in background
        try:
            from services.send_notification import notify_parents_new_record
            for sid in student_ids:
                s_name = student_names.get(sid, sid)
                def _send(s_id=sid, s_n=s_name):
                    try:
                        notify_parents_new_record(data_service, s_id, s_n, target_date)
                    except Exception as e:
                        print(f'[Scheduler] Push error for {s_id}: {e}')
                threading.Thread(target=_send, daemon=True).start()
        except Exception as e:
            print(f'[Scheduler] Notification service unavailable: {e}')

        print(f'[Scheduler] Executed schedule {schedule_id}: {len(student_ids)} students notified for {target_date}')
    except Exception as e:
        print(f'[Scheduler] Error executing schedule {schedule_id}: {e}')
    finally:
        if conn:
            conn.close()


"""Single-student contact book publish and scheduled notification support."""
import json
import os
import threading
import time
from datetime import datetime


STATUS_DRAFT = 'draft'
STATUS_NOTIFIED = 'notified'
STATUS_READ = 'read'
STATUS_SIGNED = 'signed'
SCHEDULE_PENDING = 'pending'
SCHEDULE_SENT = 'sent'
SCHEDULE_CANCELLED = 'cancelled'
SCHEDULE_FAILED = 'failed'

_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def ensure_scheduled_contact_book_notifications_table(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS scheduled_contact_book_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id VARCHAR(50) NOT NULL,
            date VARCHAR(20) NOT NULL,
            class_name VARCHAR(100),
            student_name VARCHAR(100),
            send_at VARCHAR(50) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            sent_by VARCHAR(100),
            sent_at VARCHAR(50),
            created_at VARCHAR(50) NOT NULL,
            updated_at VARCHAR(50),
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_scbn_pending_send_at
            ON scheduled_contact_book_notifications(status, send_at);
        CREATE INDEX IF NOT EXISTS idx_scbn_student_date
            ON scheduled_contact_book_notifications(student_id, date, status);
    ''')


def _canonical_contact_book_status(row):
    if not row:
        return STATUS_DRAFT

    inferred = STATUS_DRAFT
    if row['signed_at']:
        inferred = STATUS_SIGNED
    elif row['read_at']:
        inferred = STATUS_READ
    elif row['notified_at']:
        inferred = STATUS_NOTIFIED

    explicit = (row['status'] or '').strip().lower()
    rank = {
        STATUS_DRAFT: 0,
        STATUS_NOTIFIED: 1,
        STATUS_READ: 2,
        STATUS_SIGNED: 3,
    }
    if explicit not in rank:
        return inferred
    return explicit if rank[explicit] >= rank[inferred] else inferred


def _parse_date_key(date_key):
    year, month, _ = [int(part) for part in str(date_key).split('-')]
    return year, month


def dismissal_send_at_for_date(date_key):
    return datetime.strptime(f'{date_key} 16:20:00', '%Y-%m-%d %H:%M:%S')


def serialize_scheduled_notification(row):
    if not row:
        return None
    return {
        'id': row['id'],
        'studentId': row['student_id'],
        'date': row['date'],
        'className': row['class_name'] or '',
        'studentName': row['student_name'] or '',
        'sendAt': row['send_at'],
        'status': row['status'],
        'sentBy': row['sent_by'] or '',
        'sentAt': row['sent_at'],
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
        'error': row['error'] or '',
    }


def get_pending_schedule(conn, student_id, date_key):
    ensure_scheduled_contact_book_notifications_table(conn)
    return conn.execute(
        '''
        SELECT *
        FROM scheduled_contact_book_notifications
        WHERE student_id = ? AND date = ? AND status = ?
        ORDER BY send_at DESC, id DESC
        LIMIT 1
        ''',
        (student_id, date_key, SCHEDULE_PENDING),
    ).fetchone()


def get_pending_schedule_map(conn, student_id, date_keys):
    ensure_scheduled_contact_book_notifications_table(conn)
    keys = [str(key) for key in date_keys if key]
    if not keys:
        return {}

    placeholders = ','.join('?' for _ in keys)
    rows = conn.execute(
        f'''
        SELECT *
        FROM scheduled_contact_book_notifications
        WHERE student_id = ?
          AND date IN ({placeholders})
          AND status = ?
        ORDER BY send_at DESC, id DESC
        ''',
        (student_id, *keys, SCHEDULE_PENDING),
    ).fetchall()

    schedules = {}
    for row in rows:
        schedules.setdefault(row['date'], row)
    return schedules


def get_pending_schedule_map_for_date(conn, date_key):
    ensure_scheduled_contact_book_notifications_table(conn)
    rows = conn.execute(
        '''
        SELECT *
        FROM scheduled_contact_book_notifications
        WHERE date = ? AND status = ?
        ORDER BY send_at DESC, id DESC
        ''',
        (date_key, SCHEDULE_PENDING),
    ).fetchall()
    schedules = {}
    for row in rows:
        schedules.setdefault(row['student_id'], row)
    return schedules


def _cancel_pending_schedules(conn, student_id, date_key, exclude_id=None):
    now = datetime.now().isoformat()
    if exclude_id:
        conn.execute(
            '''
            UPDATE scheduled_contact_book_notifications
            SET status = ?, updated_at = ?
            WHERE student_id = ? AND date = ? AND status = ? AND id != ?
            ''',
            (SCHEDULE_CANCELLED, now, student_id, date_key, SCHEDULE_PENDING, exclude_id),
        )
    else:
        conn.execute(
            '''
            UPDATE scheduled_contact_book_notifications
            SET status = ?, updated_at = ?
            WHERE student_id = ? AND date = ? AND status = ?
            ''',
            (SCHEDULE_CANCELLED, now, student_id, date_key, SCHEDULE_PENDING),
        )


def publish_contact_book_now(
    data_service,
    student_id,
    date_key,
    class_name='',
    student_name='',
    sent_by='',
    schedule_id=None,
):
    conn = data_service.get_db()
    try:
        ensure_scheduled_contact_book_notifications_table(conn)
        now = datetime.now().isoformat()
        year, month = _parse_date_key(date_key)
        row = conn.execute(
            '''
            SELECT id, status, notified_at, read_at, signed_at
            FROM contact_books
            WHERE student_id = ? AND date = ?
            ''',
            (student_id, date_key),
        ).fetchone()

        current_status = _canonical_contact_book_status(row)
        already_published = current_status in (STATUS_NOTIFIED, STATUS_READ, STATUS_SIGNED)

        if row:
            if already_published:
                if row['status'] != current_status:
                    conn.execute(
                        'UPDATE contact_books SET status = ?, last_modified = ? WHERE id = ?',
                        (current_status, now, row['id']),
                    )
            else:
                conn.execute(
                    '''
                    UPDATE contact_books
                    SET status = ?, notified_at = COALESCE(notified_at, ?), last_modified = ?
                    WHERE id = ?
                    ''',
                    (STATUS_NOTIFIED, now, now, row['id']),
                )
        else:
            conn.execute(
                '''
                INSERT INTO contact_books (student_id, date, year, month, status, notified_at, last_modified)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (student_id, date_key, year, month, STATUS_NOTIFIED, now, now),
            )

        _cancel_pending_schedules(conn, student_id, date_key, exclude_id=schedule_id)

        if schedule_id:
            conn.execute(
                '''
                UPDATE scheduled_contact_book_notifications
                SET status = ?, sent_at = ?, updated_at = ?, error = NULL
                WHERE id = ?
                ''',
                (SCHEDULE_SENT, now, now, schedule_id),
            )

        conn.commit()
    finally:
        conn.close()

    sent_count = 0
    if not already_published:
        try:
            from services.send_notification import notify_parents_new_record
            sent_count = notify_parents_new_record(data_service, student_id, student_name or student_id, date_key)
        except Exception as e:
            print(f'[Publish] parent notification error for {student_id} {date_key}: {e}')

    return {
        'status': 'published',
        'mode': 'immediate',
        'studentId': student_id,
        'date': date_key,
        'contactBookStatus': STATUS_NOTIFIED if not already_published else current_status,
        'notifiedAt': now,
        'alreadyPublished': already_published,
        'sent': sent_count,
        'scheduledNotification': None,
    }


def schedule_contact_book_dismissal_publish(
    data_service,
    student_id,
    date_key,
    class_name='',
    student_name='',
    sent_by='',
):
    send_at = dismissal_send_at_for_date(date_key)
    if send_at <= datetime.now():
        return publish_contact_book_now(
            data_service,
            student_id,
            date_key,
            class_name=class_name,
            student_name=student_name,
            sent_by=sent_by,
        )

    conn = data_service.get_db()
    try:
        ensure_scheduled_contact_book_notifications_table(conn)
        row = conn.execute(
            '''
            SELECT id, status, notified_at, read_at, signed_at
            FROM contact_books
            WHERE student_id = ? AND date = ?
            ''',
            (student_id, date_key),
        ).fetchone()
        if not row:
            raise ValueError('Record not found')

        current_status = _canonical_contact_book_status(row)
        if current_status in (STATUS_NOTIFIED, STATUS_READ, STATUS_SIGNED):
            return {
                'status': 'already_published',
                'mode': 'dismissal',
                'studentId': student_id,
                'date': date_key,
                'contactBookStatus': current_status,
                'notifiedAt': row['notified_at'],
                'alreadyPublished': True,
                'sent': 0,
                'scheduledNotification': None,
            }

        now = datetime.now().isoformat()
        _cancel_pending_schedules(conn, student_id, date_key)
        cursor = conn.execute(
            '''
            INSERT INTO scheduled_contact_book_notifications (
                student_id, date, class_name, student_name, send_at, status,
                sent_by, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                student_id,
                date_key,
                class_name,
                student_name,
                send_at.isoformat(),
                SCHEDULE_PENDING,
                sent_by,
                now,
                now,
            ),
        )
        schedule = conn.execute(
            'SELECT * FROM scheduled_contact_book_notifications WHERE id = ?',
            (cursor.lastrowid,),
        ).fetchone()
        conn.commit()
        return {
            'status': 'scheduled',
            'mode': 'dismissal',
            'studentId': student_id,
            'date': date_key,
            'contactBookStatus': STATUS_DRAFT,
            'notifiedAt': None,
            'alreadyPublished': False,
            'sent': 0,
            'scheduledNotification': serialize_scheduled_notification(schedule),
        }
    finally:
        conn.close()


def cancel_scheduled_contact_book_publish(data_service, student_id, date_key):
    conn = data_service.get_db()
    try:
        ensure_scheduled_contact_book_notifications_table(conn)
        now = datetime.now().isoformat()
        cursor = conn.execute(
            '''
            UPDATE scheduled_contact_book_notifications
            SET status = ?, updated_at = ?
            WHERE student_id = ? AND date = ? AND status = ?
            ''',
            (SCHEDULE_CANCELLED, now, student_id, date_key, SCHEDULE_PENDING),
        )
        conn.commit()
        return {
            'status': 'cancelled',
            'studentId': student_id,
            'date': date_key,
            'cancelled': cursor.rowcount,
            'scheduledNotification': None,
        }
    finally:
        conn.close()


def process_due_contact_book_notifications(data_service, limit=50):
    conn = data_service.get_db()
    try:
        ensure_scheduled_contact_book_notifications_table(conn)
        now = datetime.now().isoformat()
        rows = conn.execute(
            '''
            SELECT *
            FROM scheduled_contact_book_notifications
            WHERE status = ? AND send_at <= ?
            ORDER BY send_at ASC, id ASC
            LIMIT ?
            ''',
            (SCHEDULE_PENDING, now, limit),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            publish_contact_book_now(
                data_service,
                row['student_id'],
                row['date'],
                class_name=row['class_name'] or '',
                student_name=row['student_name'] or '',
                sent_by=row['sent_by'] or '',
                schedule_id=row['id'],
            )
        except Exception as e:
            error = str(e)
            print(f'[PublishWorker] failed schedule {row["id"]}: {error}')
            fail_conn = data_service.get_db()
            try:
                fail_conn.execute(
                    '''
                    UPDATE scheduled_contact_book_notifications
                    SET status = ?, updated_at = ?, error = ?
                    WHERE id = ?
                    ''',
                    (SCHEDULE_FAILED, datetime.now().isoformat(), error, row['id']),
                )
                fail_conn.commit()
            finally:
                fail_conn.close()


def start_scheduled_contact_book_notification_worker(data_service, interval_seconds=30):
    global _WORKER_STARTED
    if os.environ.get('DISABLE_CONTACT_BOOK_PUBLISH_WORKER', '').lower() in ('1', 'true', 'yes'):
        return

    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True

    def _run():
        print('[PublishWorker] scheduled contact book notification worker started')
        while True:
            try:
                process_due_contact_book_notifications(data_service)
            except Exception as e:
                print(f'[PublishWorker] loop error: {e}')
            time.sleep(interval_seconds)

    threading.Thread(target=_run, daemon=True).start()

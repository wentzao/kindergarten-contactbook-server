"""Class-level notification grants — the authoritative source of "parent can view".

A grant = teacher pressed "通知家長" for one (class_name, date) at one moment.
   • Records which students were notified (explicit list, for audit)
   • Triggers per-student push (push_outbox is still per-student because each
     parent subscribes to their own child's events)
   • Cancellation is soft-delete (誤發回收 keeps audit trail)

This module replaces the old per-student `contact_book_publish_service`.
The parent app no longer relies on contact_books.status / notified_at; instead
it joins class_notification_grants to find which (class, date) pairs are
visible, then loads class_journals + contact_books for the content.
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from services.push_outbox_service import (
    enqueue_push_job,
    EVENT_CONTACT_BOOK_PARENT_UPDATE,
    ensure_push_outbox_table,
)


# ── Scheduled notification statuses ──
SCHEDULE_PENDING = 'pending'
SCHEDULE_SENT = 'sent'
SCHEDULE_CANCELLED = 'cancelled'
SCHEDULE_FAILED = 'failed'

# ── Event log transitions ──
EVENT_GRANTED = 'granted'
EVENT_CANCELLED = 'cancelled'
EVENT_SCHEDULED = 'scheduled'
EVENT_SCHEDULE_CANCELLED = 'schedule_cancelled'

# ── Event log statuses ──
STATUS_PENDING_DELIVERY = 'pending_delivery'
STATUS_DONE = 'done'
STATUS_FAILED = 'failed'

DISMISSAL_HOUR = 16
DISMISSAL_MINUTE = 20

_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def _now():
    return datetime.now().isoformat()


def _load_student_ids(raw):
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(s) for s in parsed] if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _dump_student_ids(student_ids):
    return json.dumps([str(s) for s in (student_ids or [])], ensure_ascii=False)


def ensure_grant_student_index_table(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS class_notification_grant_students (
            class_name VARCHAR(100) NOT NULL,
            date VARCHAR(20) NOT NULL,
            student_id VARCHAR(50) NOT NULL,
            created_at VARCHAR(50),
            PRIMARY KEY (class_name, date, student_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cngs_student_date
            ON class_notification_grant_students(student_id, date);
        CREATE INDEX IF NOT EXISTS idx_cngs_student_class_date
            ON class_notification_grant_students(student_id, class_name, date);
    ''')


def _replace_grant_student_index(conn, class_name, date_key, student_ids):
    ensure_grant_student_index_table(conn)
    conn.execute(
        'DELETE FROM class_notification_grant_students WHERE class_name = ? AND date = ?',
        (class_name, date_key),
    )
    values = [
        (class_name, date_key, str(sid).strip(), _now())
        for sid in student_ids
        if str(sid).strip()
    ]
    if values:
        conn.executemany(
            '''
            INSERT OR IGNORE INTO class_notification_grant_students
                (class_name, date, student_id, created_at)
            VALUES (?, ?, ?, ?)
            ''',
            values,
        )


def dismissal_send_at_for_date(date_key):
    return datetime.strptime(
        f'{date_key} {DISMISSAL_HOUR:02d}:{DISMISSAL_MINUTE:02d}:00',
        '%Y-%m-%d %H:%M:%S',
    )


# ──────────────────────────────────────────────────────────
# Serializers
# ──────────────────────────────────────────────────────────

def serialize_grant(row):
    if not row:
        return None
    return {
        'className': row['class_name'],
        'date': row['date'],
        'notifiedAt': row['notified_at'],
        'sentBy': row['sent_by'] or '',
        'studentIds': _load_student_ids(row['student_ids']),
        'cancelledAt': row['cancelled_at'],
        'cancelledBy': row['cancelled_by'] or '',
    }


def serialize_scheduled_notification(row):
    if not row:
        return None
    return {
        'id': row['id'],
        'className': row['class_name'],
        'date': row['date'],
        'studentIds': _load_student_ids(row['student_ids']),
        'sendAt': row['send_at'],
        'status': row['status'],
        'sentBy': row['sent_by'] or '',
        'sentAt': row['sent_at'],
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
        'error': row['error'] or '',
    }


def serialize_event(row):
    if not row:
        return None
    return {
        'id': row['id'],
        'className': row['class_name'],
        'date': row['date'],
        'studentIds': _load_student_ids(row['student_ids']),
        'mode': row['mode'],
        'transition': row['transition'],
        'sentBy': row['sent_by'] or '',
        'status': row['status'],
        'deliveryAttempted': bool(row['delivery_attempted']),
        'sentCount': int(row['sent_count'] or 0),
        'error': row['error'] or '',
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
    }


# ──────────────────────────────────────────────────────────
# Queries (parent app / teacher web use these to ask
# "is this (class, date) visible to parents?")
# ──────────────────────────────────────────────────────────

def get_grant(conn, class_name, date_key):
    return conn.execute(
        '''
        SELECT * FROM class_notification_grants
        WHERE class_name = ? AND date = ?
        ''',
        (class_name, date_key),
    ).fetchone()


def is_visible_to_student(grant_row, student_id):
    """A grant makes a date visible to a student iff the student is in
    student_ids AND the grant is not cancelled."""
    if not grant_row or grant_row['cancelled_at']:
        return False
    student_ids = _load_student_ids(grant_row['student_ids'])
    return str(student_id) in student_ids


def get_visible_grants_for_student(conn, student_id, class_name=None, date_like=None):
    """Return all active grants where the given student is in student_ids.
    Optional filters: class_name (exact), date_like (e.g. '2026-05-%')."""
    clauses = ['g.cancelled_at IS NULL', 'gs.student_id = ?']
    params = [str(student_id)]
    if class_name:
        clauses.append('g.class_name = ?')
        params.append(class_name)
    if date_like:
        clauses.append('g.date LIKE ?')
        params.append(date_like)
    where = ' AND '.join(clauses)
    try:
        return conn.execute(
            f'''
            SELECT g.*
            FROM class_notification_grants g
            JOIN class_notification_grant_students gs
              ON gs.class_name = g.class_name
             AND gs.date = g.date
            WHERE {where}
            ORDER BY g.date
            ''',
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        fallback_clauses = ['cancelled_at IS NULL']
        fallback_params = []
        if class_name:
            fallback_clauses.append('class_name = ?')
            fallback_params.append(class_name)
        if date_like:
            fallback_clauses.append('date LIKE ?')
            fallback_params.append(date_like)
        rows = conn.execute(
            f'SELECT * FROM class_notification_grants WHERE {" AND ".join(fallback_clauses)} ORDER BY date',
            fallback_params,
        ).fetchall()
        sid = str(student_id)
        return [r for r in rows if sid in _load_student_ids(r['student_ids'])]


def get_visible_dates_for_student(conn, student_id, class_name=None, date_like=None):
    return [r['date'] for r in get_visible_grants_for_student(
        conn, student_id, class_name=class_name, date_like=date_like,
    )]


def get_pending_schedules_for_date(conn, date_key):
    """Used by parent app to surface 'will be sent at 16:20' UI."""
    return conn.execute(
        '''
        SELECT * FROM scheduled_class_notifications
        WHERE date = ? AND status = ?
        ORDER BY send_at DESC, id DESC
        ''',
        (date_key, SCHEDULE_PENDING),
    ).fetchall()


def get_events(conn, class_name, date_key, limit=20):
    rows = conn.execute(
        '''
        SELECT * FROM class_notification_events
        WHERE class_name = ? AND date = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        ''',
        (class_name, date_key, limit),
    ).fetchall()
    return [serialize_event(r) for r in rows]


# ──────────────────────────────────────────────────────────
# Event log helpers
# ──────────────────────────────────────────────────────────

def _insert_event(
    conn,
    class_name,
    date_key,
    student_ids,
    mode,
    transition,
    sent_by,
    status,
    error=None,
    sent_count=0,
    delivery_attempted=False,
):
    now = _now()
    cursor = conn.execute(
        '''
        INSERT INTO class_notification_events (
            class_name, date, student_ids, mode, transition, sent_by,
            status, delivery_attempted, sent_count, error,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            class_name,
            date_key,
            _dump_student_ids(student_ids),
            mode,
            transition,
            sent_by,
            status,
            1 if delivery_attempted else 0,
            sent_count,
            error,
            now,
            now,
        ),
    )
    return cursor.lastrowid


def _enqueue_parent_pushes(conn, class_name, date_key, student_ids, event_id, student_names=None):
    """One push job per student → push worker fans out to each parent."""
    student_names = student_names or {}
    for sid in student_ids:
        sid = str(sid)
        name = student_names.get(sid) or sid
        enqueue_push_job(
            conn,
            EVENT_CONTACT_BOOK_PARENT_UPDATE,
            'student_parents',
            recipient_id=sid,
            payload={
                'studentId': sid,
                'studentName': name,
                'date': date_key,
                'className': class_name,
            },
            pref_column='contact_book_notify',
            idempotency_key=f'grant_push:{class_name}:{date_key}:{sid}:{event_id}',
            source_table='class_notification_events',
            source_id=event_id,
        )


def _cancel_pending_schedules(conn, class_name, date_key):
    now = _now()
    conn.execute(
        '''
        UPDATE scheduled_class_notifications
        SET status = ?, updated_at = ?
        WHERE class_name = ? AND date = ? AND status = ?
        ''',
        (SCHEDULE_CANCELLED, now, class_name, date_key, SCHEDULE_PENDING),
    )


# ──────────────────────────────────────────────────────────
# Grant / cancel (the main public API)
# ──────────────────────────────────────────────────────────

def grant_now(data_service, class_name, date_key, student_ids, sent_by='', student_names=None, mode='immediate'):
    """Mark (class, date) as visible to parents AND push notify each student's parents.

    Idempotent: re-granting the same (class, date) with overlapping students
    only pushes for newly-added students.
    """
    student_ids = [str(s) for s in (student_ids or []) if str(s).strip()]
    if not student_ids:
        raise ValueError('student_ids is required')

    now = _now()
    conn = data_service.get_db()
    try:
        ensure_push_outbox_table(conn)
        ensure_grant_student_index_table(conn)
        conn.execute('BEGIN IMMEDIATE')

        existing = get_grant(conn, class_name, date_key)

        if existing and not existing['cancelled_at']:
            # Already granted → only push for newly-added students
            existing_ids = set(_load_student_ids(existing['student_ids']))
            new_ids = [s for s in student_ids if s not in existing_ids]
            merged_ids = sorted(existing_ids.union(student_ids))

            conn.execute(
                '''
                UPDATE class_notification_grants
                SET student_ids = ?
                WHERE class_name = ? AND date = ?
                ''',
                (_dump_student_ids(merged_ids), class_name, date_key),
            )
            _replace_grant_student_index(conn, class_name, date_key, merged_ids)
            event_id = _insert_event(
                conn, class_name, date_key, new_ids,
                mode, EVENT_GRANTED, sent_by,
                STATUS_PENDING_DELIVERY if new_ids else STATUS_DONE,
                delivery_attempted=bool(new_ids),
            )
            if new_ids:
                _enqueue_parent_pushes(conn, class_name, date_key, new_ids, event_id, student_names)
            grant_row = get_grant(conn, class_name, date_key)
            already_granted = True
            pushed_ids = new_ids
        else:
            # Fresh grant (or reviving a cancelled one)
            if existing:
                conn.execute(
                    '''
                    UPDATE class_notification_grants
                    SET notified_at = ?, sent_by = ?, student_ids = ?,
                        cancelled_at = NULL, cancelled_by = NULL
                    WHERE class_name = ? AND date = ?
                    ''',
                    (now, sent_by, _dump_student_ids(student_ids), class_name, date_key),
                )
            else:
                conn.execute(
                    '''
                    INSERT INTO class_notification_grants
                        (class_name, date, notified_at, sent_by, student_ids)
                    VALUES (?, ?, ?, ?, ?)
                    ''',
                    (class_name, date_key, now, sent_by, _dump_student_ids(student_ids)),
                )
            _replace_grant_student_index(conn, class_name, date_key, student_ids)
            event_id = _insert_event(
                conn, class_name, date_key, student_ids,
                mode, EVENT_GRANTED, sent_by,
                STATUS_PENDING_DELIVERY, delivery_attempted=True,
            )
            _enqueue_parent_pushes(conn, class_name, date_key, student_ids, event_id, student_names)
            grant_row = get_grant(conn, class_name, date_key)
            already_granted = False
            pushed_ids = student_ids

        # Once granted, any pending schedule for the same (class, date) is moot.
        _cancel_pending_schedules(conn, class_name, date_key)

        conn.commit()
        return {
            'status': 'granted',
            'mode': mode,
            'alreadyGranted': already_granted,
            'grant': serialize_grant(grant_row),
            'pushedStudentIds': pushed_ids,
            'eventId': event_id,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_grant(data_service, class_name, date_key, cancelled_by=''):
    """Soft-delete a grant. Parents stop seeing this (class, date)."""
    conn = data_service.get_db()
    try:
        ensure_grant_student_index_table(conn)
        conn.execute('BEGIN IMMEDIATE')
        now = _now()
        existing = get_grant(conn, class_name, date_key)
        if not existing:
            conn.rollback()
            return {'status': 'not_found'}
        if existing['cancelled_at']:
            conn.rollback()
            return {'status': 'already_cancelled', 'grant': serialize_grant(existing)}

        conn.execute(
            '''
            UPDATE class_notification_grants
            SET cancelled_at = ?, cancelled_by = ?
            WHERE class_name = ? AND date = ?
            ''',
            (now, cancelled_by, class_name, date_key),
        )
        conn.execute(
            'DELETE FROM class_notification_grant_students WHERE class_name = ? AND date = ?',
            (class_name, date_key),
        )
        student_ids = _load_student_ids(existing['student_ids'])
        _insert_event(
            conn, class_name, date_key, student_ids,
            'cancel', EVENT_CANCELLED, cancelled_by,
            STATUS_DONE,
        )
        grant_row = get_grant(conn, class_name, date_key)
        conn.commit()
        return {'status': 'cancelled', 'grant': serialize_grant(grant_row)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# Dismissal-time scheduling (class-level)
# ──────────────────────────────────────────────────────────

def schedule_dismissal_grant(data_service, class_name, date_key, student_ids, sent_by='', student_names=None):
    student_ids = [str(s) for s in (student_ids or []) if str(s).strip()]
    if not student_ids:
        raise ValueError('student_ids is required')

    send_at = dismissal_send_at_for_date(date_key)
    if send_at <= datetime.now():
        # Past dismissal time → just grant immediately
        return grant_now(
            data_service, class_name, date_key, student_ids,
            sent_by=sent_by, student_names=student_names, mode='dismissal_immediate',
        )

    conn = data_service.get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')

        # If already granted, no need to schedule
        existing = get_grant(conn, class_name, date_key)
        if existing and not existing['cancelled_at']:
            conn.rollback()
            return {
                'status': 'already_granted',
                'grant': serialize_grant(existing),
                'scheduledNotification': None,
            }

        _cancel_pending_schedules(conn, class_name, date_key)
        now = _now()
        cursor = conn.execute(
            '''
            INSERT INTO scheduled_class_notifications
                (class_name, date, student_ids, send_at, status, sent_by,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                class_name, date_key, _dump_student_ids(student_ids),
                send_at.isoformat(), SCHEDULE_PENDING, sent_by, now, now,
            ),
        )
        schedule = conn.execute(
            'SELECT * FROM scheduled_class_notifications WHERE id = ?',
            (cursor.lastrowid,),
        ).fetchone()
        _insert_event(
            conn, class_name, date_key, student_ids,
            'dismissal', EVENT_SCHEDULED, sent_by, STATUS_DONE,
        )
        conn.commit()
        return {
            'status': 'scheduled',
            'grant': None,
            'scheduledNotification': serialize_scheduled_notification(schedule),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_scheduled_grant(data_service, class_name, date_key, cancelled_by=''):
    conn = data_service.get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        now = _now()
        cursor = conn.execute(
            '''
            UPDATE scheduled_class_notifications
            SET status = ?, updated_at = ?
            WHERE class_name = ? AND date = ? AND status = ?
            ''',
            (SCHEDULE_CANCELLED, now, class_name, date_key, SCHEDULE_PENDING),
        )
        _insert_event(
            conn, class_name, date_key, [],
            'cancel_schedule', EVENT_SCHEDULE_CANCELLED, cancelled_by, STATUS_DONE,
        )
        conn.commit()
        return {'status': 'cancelled', 'rowsAffected': cursor.rowcount}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────
# Background worker — sends scheduled dismissal-time grants
# ──────────────────────────────────────────────────────────

def process_due_scheduled_notifications(data_service, limit=50):
    conn = data_service.get_db()
    try:
        now = _now()
        rows = conn.execute(
            '''
            SELECT * FROM scheduled_class_notifications
            WHERE status = ? AND send_at <= ?
            ORDER BY send_at ASC, id ASC
            LIMIT ?
            ''',
            (SCHEDULE_PENDING, now, limit),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        sid_list = _load_student_ids(row['student_ids'])
        schedule_id = row['id']
        try:
            grant_now(
                data_service,
                row['class_name'],
                row['date'],
                sid_list,
                sent_by=row['sent_by'] or '',
                mode='dismissal_worker',
            )
            mark_conn = data_service.get_db()
            try:
                mark_conn.execute(
                    '''
                    UPDATE scheduled_class_notifications
                    SET status = ?, sent_at = ?, updated_at = ?, error = NULL
                    WHERE id = ?
                    ''',
                    (SCHEDULE_SENT, _now(), _now(), schedule_id),
                )
                mark_conn.commit()
            finally:
                mark_conn.close()
        except Exception as e:
            err = str(e)
            print(f'[GrantWorker] failed schedule {schedule_id}: {err}')
            fail_conn = data_service.get_db()
            try:
                fail_conn.execute(
                    '''
                    UPDATE scheduled_class_notifications
                    SET status = ?, updated_at = ?, error = ?
                    WHERE id = ?
                    ''',
                    (SCHEDULE_FAILED, _now(), err, schedule_id),
                )
                fail_conn.commit()
            finally:
                fail_conn.close()


def start_scheduled_notification_worker(data_service, interval_seconds=30):
    global _WORKER_STARTED
    if os.environ.get('DISABLE_NOTIFICATION_GRANT_WORKER', '').lower() in ('1', 'true', 'yes'):
        return

    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True

    def _run():
        print('[GrantWorker] scheduled class notification worker started')
        while True:
            time.sleep(interval_seconds)
            try:
                process_due_scheduled_notifications(data_service)
            except Exception as e:
                print(f'[GrantWorker] loop error: {e}')

    threading.Thread(target=_run, daemon=True).start()

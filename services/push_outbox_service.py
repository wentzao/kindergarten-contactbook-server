"""Durable push notification outbox.

The outbox keeps local state transitions and external push side effects from
drifting apart when the process restarts or the network fails mid-request.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta


OUTBOX_PENDING = 'pending'
OUTBOX_SENDING = 'sending'
OUTBOX_SENT = 'sent'
OUTBOX_FAILED = 'failed'
OUTBOX_CANCELLED = 'cancelled'

EVENT_CONTACT_BOOK_PARENT_UPDATE = 'contact_book_parent_update'
EVENT_ANNOUNCEMENT = 'announcement'
EVENT_ANNOUNCEMENT_UPDATE = 'announcement_update'
EVENT_CONTACT_BOOK_PARENT_COMMENT = 'contact_book_parent_comment'
EVENT_CONTACT_BOOK_TEACHER_COMMENT = 'contact_book_teacher_comment'
EVENT_CONTACT_BOOK_TEACHER_STATUS = 'contact_book_teacher_status'
EVENT_CONTACT_BOOK_PARENT_COMMENT_DELETED = 'contact_book_parent_comment_deleted'
EVENT_CONTACT_BOOK_TEACHER_COMMENT_DELETED = 'contact_book_teacher_comment_deleted'
EVENT_ROLE_PUSH = 'role_push'
EVENT_TEACHER_USER_IDS_PUSH = 'teacher_user_ids_push'

_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def ensure_push_outbox_table(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS push_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type VARCHAR(80) NOT NULL,
            recipient_scope VARCHAR(80) NOT NULL,
            recipient_id VARCHAR(100),
            title TEXT,
            body TEXT,
            payload TEXT NOT NULL,
            pref_column VARCHAR(80),
            idempotency_key VARCHAR(200) NOT NULL UNIQUE,
            source_table VARCHAR(80),
            source_id INTEGER,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            next_attempt_at VARCHAR(50) NOT NULL,
            sent_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at VARCHAR(50) NOT NULL,
            updated_at VARCHAR(50),
            sent_at VARCHAR(50)
        );
        CREATE INDEX IF NOT EXISTS idx_push_outbox_due
            ON push_outbox(status, next_attempt_at);
        CREATE INDEX IF NOT EXISTS idx_push_outbox_source
            ON push_outbox(source_table, source_id);
    ''')


def enqueue_push_job(
    conn,
    event_type,
    recipient_scope,
    recipient_id='',
    payload=None,
    idempotency_key=None,
    pref_column=None,
    source_table=None,
    source_id=None,
):
    """Enqueue one push job in the caller transaction."""
    ensure_push_outbox_table(conn)
    now = datetime.now().isoformat()
    normalized_payload = payload or {}
    key = idempotency_key or f'{event_type}:{recipient_scope}:{recipient_id}:{now}'
    conn.execute(
        '''
        INSERT INTO push_outbox (
            event_type, recipient_scope, recipient_id, title, body, payload,
            pref_column, idempotency_key, source_table, source_id, status,
            next_attempt_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            event_type,
            recipient_scope,
            str(recipient_id or ''),
            '',
            '',
            json.dumps(normalized_payload, ensure_ascii=False),
            pref_column,
            key,
            source_table,
            source_id,
            OUTBOX_PENDING,
            now,
            now,
            now,
        ),
    )


def enqueue_contact_book_parent_update(
    conn,
    publish_event_id,
    student_id,
    student_name,
    date_key,
):
    """Enqueue a parent contact-book update notification in the caller transaction."""
    payload = {
        'studentId': str(student_id),
        'studentName': str(student_name or student_id),
        'date': str(date_key),
    }
    enqueue_push_job(
        conn,
        EVENT_CONTACT_BOOK_PARENT_UPDATE,
        'student_parents',
        recipient_id=student_id,
        payload=payload,
        pref_column='contact_book_notify',
        idempotency_key=f'contact_book_publish:{publish_event_id}',
        source_table='contact_book_publish_events',
        source_id=publish_event_id,
    )


def _row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def serialize_push_outbox_job(row):
    if not row:
        return None
    payload = {}
    try:
        payload = json.loads(row['payload'] or '{}')
    except (json.JSONDecodeError, TypeError):
        payload = {'_raw': row['payload'] or ''}
    return {
        'id': row['id'],
        'eventType': row['event_type'],
        'recipientScope': row['recipient_scope'],
        'recipientId': row['recipient_id'] or '',
        'payload': payload,
        'prefColumn': row['pref_column'] or '',
        'idempotencyKey': row['idempotency_key'],
        'sourceTable': row['source_table'] or '',
        'sourceId': row['source_id'],
        'status': row['status'],
        'attempts': int(row['attempts'] or 0),
        'maxAttempts': int(row['max_attempts'] or 0),
        'nextAttemptAt': row['next_attempt_at'],
        'sentCount': int(row['sent_count'] or 0),
        'lastError': row['last_error'] or '',
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
        'sentAt': row['sent_at'],
    }


def get_push_outbox_summary(conn):
    ensure_push_outbox_table(conn)
    rows = conn.execute(
        '''
        SELECT status, COUNT(*) AS count
        FROM push_outbox
        GROUP BY status
        ORDER BY status
        '''
    ).fetchall()
    by_status = {row['status']: int(row['count'] or 0) for row in rows}
    failed_rows = conn.execute(
        '''
        SELECT *
        FROM push_outbox
        WHERE status = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 10
        ''',
        (OUTBOX_FAILED,),
    ).fetchall()
    pending_due = conn.execute(
        '''
        SELECT COUNT(*) AS count
        FROM push_outbox
        WHERE status = ? AND next_attempt_at <= ?
        ''',
        (OUTBOX_PENDING, datetime.now().isoformat()),
    ).fetchone()
    return {
        'byStatus': by_status,
        'pendingDue': int(pending_due['count'] or 0),
        'recentFailed': [serialize_push_outbox_job(row) for row in failed_rows],
    }


def list_push_outbox_jobs(conn, statuses=None, event_type=None, limit=100):
    ensure_push_outbox_table(conn)
    limit = min(max(int(limit or 100), 1), 500)
    clauses = []
    params = []
    if statuses:
        normalized = [str(status).strip() for status in statuses if str(status).strip()]
        if normalized:
            placeholders = ','.join('?' for _ in normalized)
            clauses.append(f'status IN ({placeholders})')
            params.extend(normalized)
    if event_type:
        clauses.append('event_type = ?')
        params.append(str(event_type).strip())

    where_sql = f'WHERE {" AND ".join(clauses)}' if clauses else ''
    rows = conn.execute(
        f'''
        SELECT *
        FROM push_outbox
        {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        ''',
        (*params, limit),
    ).fetchall()
    return [serialize_push_outbox_job(row) for row in rows]


def retry_push_outbox_job(conn, job_id):
    ensure_push_outbox_table(conn)
    now = datetime.now().isoformat()
    cursor = conn.execute(
        '''
        UPDATE push_outbox
        SET status = ?, attempts = 0, next_attempt_at = ?, last_error = NULL, updated_at = ?
        WHERE id = ? AND status IN (?, ?, ?)
        ''',
        (OUTBOX_PENDING, now, now, job_id, OUTBOX_FAILED, OUTBOX_SENDING, OUTBOX_CANCELLED),
    )
    row = conn.execute('SELECT * FROM push_outbox WHERE id = ?', (job_id,)).fetchone()
    return cursor.rowcount, serialize_push_outbox_job(row)


def retry_failed_push_outbox_jobs(conn, limit=100):
    ensure_push_outbox_table(conn)
    limit = min(max(int(limit or 100), 1), 500)
    now = datetime.now().isoformat()
    rows = conn.execute(
        '''
        SELECT id
        FROM push_outbox
        WHERE status = ?
        ORDER BY updated_at ASC, id ASC
        LIMIT ?
        ''',
        (OUTBOX_FAILED, limit),
    ).fetchall()
    ids = [row['id'] for row in rows]
    if not ids:
        return 0, []

    placeholders = ','.join('?' for _ in ids)
    conn.execute(
        f'''
        UPDATE push_outbox
        SET status = ?, attempts = 0, next_attempt_at = ?, last_error = NULL, updated_at = ?
        WHERE id IN ({placeholders})
        ''',
        (OUTBOX_PENDING, now, now, *ids),
    )
    return len(ids), ids


def cancel_push_outbox_job(conn, job_id):
    ensure_push_outbox_table(conn)
    now = datetime.now().isoformat()
    cursor = conn.execute(
        '''
        UPDATE push_outbox
        SET status = ?, updated_at = ?
        WHERE id = ? AND status IN (?, ?, ?)
        ''',
        (OUTBOX_CANCELLED, now, job_id, OUTBOX_PENDING, OUTBOX_SENDING, OUTBOX_FAILED),
    )
    row = conn.execute('SELECT * FROM push_outbox WHERE id = ?', (job_id,)).fetchone()
    return cursor.rowcount, serialize_push_outbox_job(row)


def _claim_next_job(data_service):
    conn = data_service.get_db()
    try:
        ensure_push_outbox_table(conn)
        conn.execute('BEGIN IMMEDIATE')
        now = datetime.now().isoformat()
        row = conn.execute(
            '''
            SELECT *
            FROM push_outbox
            WHERE status = ?
              AND next_attempt_at <= ?
              AND attempts < max_attempts
            ORDER BY next_attempt_at ASC, id ASC
            LIMIT 1
            ''',
            (OUTBOX_PENDING, now),
        ).fetchone()
        if not row:
            conn.commit()
            return None

        conn.execute(
            '''
            UPDATE push_outbox
            SET status = ?, attempts = attempts + 1, updated_at = ?
            WHERE id = ? AND status = ?
            ''',
            (OUTBOX_SENDING, now, row['id'], OUTBOX_PENDING),
        )
        claimed = conn.execute('SELECT * FROM push_outbox WHERE id = ?', (row['id'],)).fetchone()
        conn.commit()
        return _row_to_dict(claimed)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mark_publish_event_delivery(data_service, source_id, status, sent_count=0, error=None):
    if not source_id:
        return
    conn = data_service.get_db()
    try:
        conn.execute(
            '''
            UPDATE contact_book_publish_events
            SET status = ?, delivery_attempted = 1, sent_count = ?, error = ?, updated_at = ?
            WHERE id = ?
            ''',
            (status, sent_count, error, datetime.now().isoformat(), source_id),
        )
        conn.commit()
    finally:
        conn.close()


def _send_job(data_service, job):
    payload = json.loads(job['payload'] or '{}')
    if job['event_type'] == EVENT_CONTACT_BOOK_PARENT_UPDATE:
        from services.send_notification import notify_parents_new_record

        return notify_parents_new_record(
            data_service,
            payload.get('studentId') or job['recipient_id'],
            payload.get('studentName') or payload.get('studentId') or job['recipient_id'],
            payload.get('date') or '',
        )
    if job['event_type'] == EVENT_ANNOUNCEMENT:
        from services.send_notification import notify_parents_announcement

        return notify_parents_announcement(
            data_service,
            payload.get('newsId') or job['recipient_id'],
            payload.get('title') or '',
            payload.get('body') or '',
        )
    if job['event_type'] == EVENT_ANNOUNCEMENT_UPDATE:
        from services.send_notification import notify_parents_announcement_update

        return notify_parents_announcement_update(
            data_service,
            payload.get('newsId') or job['recipient_id'],
        )
    if job['event_type'] == EVENT_CONTACT_BOOK_PARENT_COMMENT:
        from services.send_notification import notify_parents_new_comment

        return notify_parents_new_comment(
            data_service,
            payload.get('studentId') or job['recipient_id'],
            payload.get('studentName') or payload.get('studentId') or job['recipient_id'],
            payload.get('senderName') or '老師',
            payload.get('content') or '',
            payload.get('date') or '',
        )
    if job['event_type'] == EVENT_CONTACT_BOOK_TEACHER_COMMENT:
        from services.send_notification import notify_teachers_new_comment

        return notify_teachers_new_comment(
            data_service,
            payload.get('studentId') or job['recipient_id'],
            payload.get('studentName') or payload.get('studentId') or job['recipient_id'],
            payload.get('senderName') or '家長',
            payload.get('content') or '',
            payload.get('date') or '',
            class_name=payload.get('className') or None,
        )
    if job['event_type'] == EVENT_CONTACT_BOOK_TEACHER_STATUS:
        from services.send_notification import notify_teachers_status_update

        return notify_teachers_status_update(
            data_service,
            payload.get('studentId') or job['recipient_id'],
            payload.get('studentName') or payload.get('studentId') or job['recipient_id'],
            payload.get('date') or '',
            payload.get('status') or '',
        )
    if job['event_type'] == EVENT_CONTACT_BOOK_PARENT_COMMENT_DELETED:
        from services.send_notification import notify_parents_comment_deleted

        return notify_parents_comment_deleted(
            data_service,
            payload.get('studentId') or job['recipient_id'],
            payload.get('date') or '',
            payload.get('imageUrl') or '',
        )
    if job['event_type'] == EVENT_CONTACT_BOOK_TEACHER_COMMENT_DELETED:
        from services.send_notification import notify_teachers_comment_deleted

        return notify_teachers_comment_deleted(
            data_service,
            payload.get('studentId') or job['recipient_id'],
            payload.get('date') or '',
            payload.get('content') or '',
            payload.get('senderName') or '家長',
            class_name=payload.get('className') or None,
        )
    if job['event_type'] == EVENT_ROLE_PUSH:
        from services.send_notification import send_to_role

        count = 0
        for role in payload.get('roles') or []:
            count += send_to_role(
                data_service,
                role,
                payload.get('title') or '',
                payload.get('body') or '',
                payload.get('data') or {},
            )
        return count
    if job['event_type'] == EVENT_TEACHER_USER_IDS_PUSH:
        from services.send_notification import send_to_teacher_user_ids

        return send_to_teacher_user_ids(
            data_service,
            payload.get('userIds') or [],
            payload.get('title') or '',
            payload.get('body') or '',
            payload.get('data') or {},
        )

    raise ValueError(f'Unsupported push outbox event type: {job["event_type"]}')


def _mark_job_sent(data_service, job, sent_count):
    now = datetime.now().isoformat()
    conn = data_service.get_db()
    try:
        conn.execute(
            '''
            UPDATE push_outbox
            SET status = ?, sent_count = ?, last_error = NULL, updated_at = ?, sent_at = ?
            WHERE id = ?
            ''',
            (OUTBOX_SENT, sent_count, now, now, job['id']),
        )
        conn.commit()
    finally:
        conn.close()
    if job.get('source_table') == 'contact_book_publish_events':
        _mark_publish_event_delivery(data_service, job.get('source_id'), 'sent', sent_count, None)


def _mark_job_failed(data_service, job, error):
    attempts = int(job.get('attempts') or 0)
    max_attempts = int(job.get('max_attempts') or 5)
    terminal = attempts >= max_attempts
    status = OUTBOX_FAILED if terminal else OUTBOX_PENDING
    delay_seconds = min(300, 10 * (2 ** max(0, attempts - 1)))
    next_attempt_at = datetime.now() + timedelta(seconds=delay_seconds)
    now = datetime.now().isoformat()

    conn = data_service.get_db()
    try:
        conn.execute(
            '''
            UPDATE push_outbox
            SET status = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            ''',
            (status, next_attempt_at.isoformat(), error, now, job['id']),
        )
        conn.commit()
    finally:
        conn.close()

    if job.get('source_table') == 'contact_book_publish_events':
        event_status = 'delivery_failed' if terminal else 'pending_delivery'
        _mark_publish_event_delivery(data_service, job.get('source_id'), event_status, 0, error)


def _reset_stale_sending_jobs(data_service, stale_after_seconds=600):
    cutoff = (datetime.now() - timedelta(seconds=stale_after_seconds)).isoformat()
    now = datetime.now().isoformat()
    conn = data_service.get_db()
    try:
        ensure_push_outbox_table(conn)
        conn.execute(
            '''
            UPDATE push_outbox
            SET status = ?, last_error = ?, updated_at = ?
            WHERE status = ? AND updated_at <= ? AND attempts >= max_attempts
            ''',
            (OUTBOX_FAILED, 'stale sending job exceeded max attempts', now, OUTBOX_SENDING, cutoff),
        )
        conn.execute(
            '''
            UPDATE push_outbox
            SET status = ?, next_attempt_at = ?, updated_at = ?
            WHERE status = ? AND updated_at <= ? AND attempts < max_attempts
            ''',
            (OUTBOX_PENDING, now, now, OUTBOX_SENDING, cutoff),
        )
        conn.commit()
    finally:
        conn.close()


def process_push_outbox(data_service, limit=50):
    _reset_stale_sending_jobs(data_service)
    processed = 0
    while processed < limit:
        job = _claim_next_job(data_service)
        if not job:
            break
        try:
            sent_count = _send_job(data_service, job)
            _mark_job_sent(data_service, job, sent_count)
        except Exception as e:
            error = str(e)
            print(f'[PushOutbox] job {job["id"]} failed: {error}')
            _mark_job_failed(data_service, job, error)
        processed += 1
    return processed


def start_push_outbox_worker(data_service, interval_seconds=10):
    global _WORKER_STARTED
    if os.environ.get('DISABLE_PUSH_OUTBOX_WORKER', '').lower() in ('1', 'true', 'yes'):
        return

    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True

    def _run():
        print('[PushOutbox] worker started')
        while True:
            try:
                process_push_outbox(data_service)
            except Exception as e:
                print(f'[PushOutbox] loop error: {e}')
            time.sleep(interval_seconds)

    threading.Thread(target=_run, daemon=True).start()

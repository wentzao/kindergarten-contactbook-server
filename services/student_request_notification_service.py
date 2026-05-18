"""Teacher notification helpers for parent-submitted student requests."""
from datetime import datetime

from services.push_outbox_service import (
    EVENT_STUDENT_LEAVE_REQUEST,
    EVENT_STUDENT_MED_REQUEST,
    enqueue_push_job,
)


def enqueue_student_request_notification(data_service, request_type, record):
    """Enqueue a durable teacher push for a leave or medication request."""
    student_id = str(record.get('childId') or record.get('child_id') or '').strip()
    if not student_id:
        return

    normalized_type = str(request_type or '').strip()
    event_type = EVENT_STUDENT_MED_REQUEST if normalized_type == 'med' else EVENT_STUDENT_LEAVE_REQUEST
    record_id = str(record.get('id') or '').strip()
    date_key = _request_date(record)
    class_name = _lookup_latest_class_name(data_service, student_id)

    payload = {
        'studentId': student_id,
        'recordId': record_id,
        'date': date_key,
        'className': class_name,
        'requestKind': normalized_type,
    }

    conn = data_service.get_db()
    try:
        enqueue_push_job(
            conn,
            event_type,
            'responsible_teachers',
            recipient_id=student_id,
            payload=payload,
            idempotency_key=f'{event_type}:{record_id or student_id}:{date_key}',
            pref_column='contact_book_notify',
            source_table='leave_records' if event_type == EVENT_STUDENT_LEAVE_REQUEST else 'med_records',
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _request_date(record):
    raw = (
        record.get('startDate')
        or record.get('date')
        or str(record.get('createdAt') or '')[:10]
    )
    raw = str(raw or '').strip()
    if raw:
        return raw
    return datetime.now().date().isoformat()


def _lookup_latest_class_name(data_service, student_id):
    conn = data_service.get_db()
    try:
        row = conn.execute(
            '''
            SELECT class_name
            FROM student_class_cache
            WHERE student_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            ''',
            (student_id,),
        ).fetchone()
        return str(row['class_name'] or '') if row else ''
    except Exception:
        return ''
    finally:
        conn.close()

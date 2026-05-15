"""Server-side inbox for native teacher notifications."""
import json
from datetime import datetime


def ensure_teacher_notifications_table(conn):
    """Create the teacher notification inbox table if it does not exist."""
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS teacher_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_user_id VARCHAR(100) NOT NULL,
            type VARCHAR(100) NOT NULL,
            title VARCHAR(200),
            body TEXT,
            student_id VARCHAR(50),
            date VARCHAR(20),
            class_name VARCHAR(100),
            status VARCHAR(50),
            payload TEXT,
            read_at VARCHAR(50),
            created_at VARCHAR(50) NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_teacher_notifications_recipient_created
            ON teacher_notifications(recipient_user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_teacher_notifications_recipient_read
            ON teacher_notifications(recipient_user_id, read_at);
    ''')


def _payload_value(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def create_teacher_notifications(conn, recipient_user_ids, title, body, payload=None):
    """Insert one inbox row per teacher/admin user and return ids + unread counts."""
    payload = payload or {}
    normalized_user_ids = sorted({str(uid).strip() for uid in recipient_user_ids if str(uid).strip()})
    if not normalized_user_ids:
        return {}

    ensure_teacher_notifications_table(conn)

    now = datetime.now().isoformat()
    notification_type = _payload_value(payload, 'type') or 'notification'
    payload_json = json.dumps(payload, ensure_ascii=False)
    student_id = _payload_value(payload, 'studentId', 'student_id')
    date = _payload_value(payload, 'date', 'dateKey', 'date_key')
    class_name = _payload_value(payload, 'className', 'class_name')
    status = _payload_value(payload, 'status')

    notification_ids = {}
    for user_id in normalized_user_ids:
        cursor = conn.execute(
            '''
            INSERT INTO teacher_notifications (
                recipient_user_id, type, title, body, student_id, date,
                class_name, status, payload, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                user_id,
                notification_type,
                title or '',
                body or '',
                student_id,
                date,
                class_name,
                status,
                payload_json,
                now,
            ),
        )
        notification_ids[user_id] = cursor.lastrowid

    unread_counts = get_unread_counts(conn, normalized_user_ids)
    return {
        user_id: {
            'notificationId': notification_ids.get(user_id),
            'unreadCount': unread_counts.get(user_id, 0),
        }
        for user_id in normalized_user_ids
    }


def get_unread_count(conn, user_id):
    ensure_teacher_notifications_table(conn)
    row = conn.execute(
        '''
        SELECT COUNT(*) AS count
        FROM teacher_notifications
        WHERE recipient_user_id = ? AND read_at IS NULL
        ''',
        (str(user_id),),
    ).fetchone()
    return int(row['count'] if row else 0)


def get_unread_counts(conn, user_ids):
    normalized_user_ids = sorted({str(uid).strip() for uid in user_ids if str(uid).strip()})
    if not normalized_user_ids:
        return {}

    ensure_teacher_notifications_table(conn)
    placeholders = ','.join('?' for _ in normalized_user_ids)
    rows = conn.execute(
        f'''
        SELECT recipient_user_id, COUNT(*) AS count
        FROM teacher_notifications
        WHERE recipient_user_id IN ({placeholders}) AND read_at IS NULL
        GROUP BY recipient_user_id
        ''',
        normalized_user_ids,
    ).fetchall()
    counts = {user_id: 0 for user_id in normalized_user_ids}
    counts.update({r['recipient_user_id']: int(r['count']) for r in rows})
    return counts


def serialize_teacher_notification(row):
    payload = {}
    try:
        payload = json.loads(row['payload']) if row['payload'] else {}
    except (TypeError, json.JSONDecodeError):
        payload = {}

    return {
        'id': row['id'],
        'recipientUserId': row['recipient_user_id'],
        'type': row['type'],
        'title': row['title'] or '',
        'body': row['body'] or '',
        'studentId': row['student_id'] or '',
        'date': row['date'] or '',
        'className': row['class_name'] or '',
        'status': row['status'] or '',
        'payload': payload,
        'readAt': row['read_at'],
        'createdAt': row['created_at'],
    }

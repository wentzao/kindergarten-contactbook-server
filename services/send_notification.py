"""
FCM Notification Sender Service
Uses Firebase Cloud Messaging HTTP v1 API directly (no firebase-admin SDK needed).
Only requires: requests, google-auth
"""
import os
import json
import sqlite3
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'kindergarten.db')


def lookup_student_name(student_id):
    """Look up cached student name (Chinese + English) by student_id.
    Returns the student_id itself if no name is found."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            'SELECT chinese_name, english_name FROM student_names WHERE student_id = ?',
            (student_id,)
        ).fetchone()
        conn.close()
        if row:
            name = f"{row['chinese_name'] or ''} {row['english_name'] or ''}".strip()
            return name if name else student_id
    except Exception:
        pass
    return student_id

# FCM HTTP v1 API endpoint
_PROJECT_ID = None
_CREDENTIALS = None
_FCM_URL = None

def _init():
    global _PROJECT_ID, _CREDENTIALS, _FCM_URL
    if _CREDENTIALS:
        return True
    
    key_path = os.environ.get(
        'FIREBASE_SERVICE_ACCOUNT_KEY',
        os.path.join(os.path.dirname(os.path.dirname(__file__)), 'firebase-service-account.json')
    )
    
    if not os.path.exists(key_path):
        print(f'[FCM] Service account key not found: {key_path}')
        return False
    
    with open(key_path) as f:
        key_data = json.load(f)
    
    _PROJECT_ID = key_data.get('project_id')
    _FCM_URL = f'https://fcm.googleapis.com/v1/projects/{_PROJECT_ID}/messages:send'
    
    _CREDENTIALS = service_account.Credentials.from_service_account_file(
        key_path,
        scopes=['https://www.googleapis.com/auth/firebase.messaging']
    )
    
    print(f'[FCM] Initialized for project: {_PROJECT_ID}')
    return True


def _get_access_token():
    """Get a valid access token, refreshing if needed."""
    if not _CREDENTIALS:
        return None
    _CREDENTIALS.refresh(GoogleAuthRequest())
    return _CREDENTIALS.token


def send_to_tokens(tokens, title, body, data=None):
    """
    Send a push notification to a list of tokens.
    Automatically detects Expo Push Tokens vs FCM tokens and uses the appropriate API.
    If title is empty, sends a data-only (silent) message with no visible notification.
    Returns the number of successfully sent messages.
    """
    if not tokens:
        print('[Notification] No tokens to send to')
        return 0

    # Split tokens by type
    expo_tokens = [t for t in tokens if t.startswith('ExponentPushToken[')]
    fcm_tokens = [t for t in tokens if not t.startswith('ExponentPushToken[')]

    print(f'[Notification] Sending to {len(expo_tokens)} Expo tokens, {len(fcm_tokens)} FCM tokens')
    print(f'[Notification] Title: {title}, Body: {body[:50] if body else "(empty)"}')

    count = 0
    if expo_tokens:
        count += _send_expo_push(expo_tokens, title, body, data)
    if fcm_tokens:
        count += _send_fcm(fcm_tokens, title, body, data)

    print(f'[Notification] Total sent: {count}/{len(tokens)}')
    return count


def _send_expo_push(tokens, title, body, data=None):
    """Send notifications via Expo Push Service."""
    import threading

    messages = []
    for token in tokens:
        msg = {
            'to': token,
            'data': data or {},
        }
        if title:
            msg['title'] = title
            msg['body'] = body
            msg['sound'] = 'default'
        messages.append(msg)

    # Expo allows up to 100 messages per request
    success_count = 0
    ticket_ids = []
    for i in range(0, len(messages), 100):
        batch = messages[i:i+100]
        try:
            resp = requests.post(
                'https://exp.host/--/api/v2/push/send',
                headers={'Content-Type': 'application/json'},
                json=batch,
                timeout=15,
            )
            print(f'[ExpoPush] Response: {resp.status_code} {resp.text[:500]}')
            if resp.status_code == 200:
                result = resp.json()
                tickets = result.get('data', [])
                for ticket in tickets:
                    if ticket.get('status') == 'ok':
                        success_count += 1
                        tid = ticket.get('id')
                        if tid:
                            ticket_ids.append(tid)
                    else:
                        detail = ticket.get('details', {})
                        err = detail.get('error', ticket.get('message', 'unknown'))
                        print(f'[ExpoPush] Ticket error: {err}')
            else:
                print(f'[ExpoPush] Send failed ({resp.status_code}): {resp.text[:200]}')
        except Exception as e:
            print(f'[ExpoPush] Request error: {e}')

    if success_count > 0:
        print(f'[ExpoPush] Sent {success_count}/{len(tokens)} messages, ticket_ids={ticket_ids}')

    # Check receipts after 15 seconds in background
    if ticket_ids:
        def _check_receipts():
            import time
            time.sleep(15)
            try:
                resp = requests.post(
                    'https://exp.host/--/api/v2/push/getReceipts',
                    headers={'Content-Type': 'application/json'},
                    json={'ids': ticket_ids},
                    timeout=15,
                )
                print(f'[ExpoPush] Receipts: {resp.text[:500]}')
            except Exception as e:
                print(f'[ExpoPush] Receipt check error: {e}')
        threading.Thread(target=_check_receipts, daemon=True).start()

    return success_count


def _send_fcm(tokens, title, body, data=None):
    """Send notifications via FCM HTTP v1 API."""
    if not _init():
        return 0

    access_token = _get_access_token()
    if not access_token:
        return 0

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
    }

    success_count = 0
    for token in tokens:
        msg_body = {
            'token': token,
            'data': data or {},
        }
        # Only add notification block for visible notifications (title present)
        if title:
            msg_body['notification'] = {
                'title': title,
                'body': body,
            }

        payload = {'message': msg_body}

        try:
            resp = requests.post(_FCM_URL, headers=headers, json=payload, timeout=10)
            if resp.status_code == 200:
                success_count += 1
            elif resp.status_code == 404 or 'UNREGISTERED' in resp.text:
                print(f'[FCM] Token expired: {token[:20]}...')
            else:
                print(f'[FCM] Send failed ({resp.status_code}): {resp.text[:200]}')
        except Exception as e:
            print(f'[FCM] Request error: {e}')

    return success_count


def send_to_role(data_service, role, title, body, data=None):
    """Send notification to all users with a given role."""
    conn = data_service.get_db()
    try:
        rows = conn.execute(
            'SELECT DISTINCT push_token FROM push_tokens WHERE role = ?',
            (role,)
        ).fetchall()
        tokens = [r['push_token'] for r in rows]
        if tokens:
            return send_to_tokens(tokens, title, body, data)
        return 0
    finally:
        conn.close()


def send_to_student_parents(data_service, student_id, title, body, data=None, pref_column=None):
    """Send notification to all parent devices that are linked to a specific student.

    If pref_column is provided (e.g. 'contact_book_notify'), users who have opted out
    of that notification type will be excluded.
    """
    print(f'[Notification] send_to_student_parents called for student_id={student_id}, pref_column={pref_column}')
    conn = data_service.get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT push_token, user_id, student_ids FROM push_tokens WHERE role = 'parent' AND student_ids IS NOT NULL",
        ).fetchall()
        print(f'[Notification] Found {len(rows)} parent token rows in DB')

        # Build set of opted-out user_ids
        opted_out = set()
        if pref_column:
            pref_rows = conn.execute(
                f"SELECT user_id FROM notification_preferences WHERE {pref_column} = 0"
            ).fetchall()
            opted_out = {r['user_id'] for r in pref_rows}
            print(f'[Notification] Opted-out users: {len(opted_out)}')

        tokens = []
        for r in rows:
            try:
                sids = json.loads(r['student_ids']) if r['student_ids'] else []
                print(f'[Notification] Token user_id={r["user_id"]}, student_ids={sids}, target={student_id}, match={student_id in sids}')
                if student_id in sids:
                    if pref_column and r['user_id'] in opted_out:
                        print(f'[Notification] Skipping user {r["user_id"]} (opted out)')
                        continue
                    tokens.append(r['push_token'])
            except (json.JSONDecodeError, TypeError):
                continue
        print(f'[Notification] Final tokens to send: {len(tokens)}')
        if tokens:
            return send_to_tokens(tokens, title, body, data)
        return 0
    finally:
        conn.close()


def notify_teachers_new_comment(data_service, student_id, student_name, sender_name, content, date=''):
    """Notify all teachers and admins when a parent leaves a comment."""
    # If content is a Firebase Storage URL, show a friendly label instead of the raw URL
    is_image = (
        isinstance(content, str) and (
            content.startswith('https://firebasestorage.googleapis.com') or
            content.startswith('https://storage.googleapis.com')
        )
    )
    content_preview = '傳了一張照片 📷' if is_image else content[:100]

    title = f'💬 {student_name} 的聯絡簿有新留言'
    body = f'{sender_name}: {content_preview}'
    data = {
        'type': 'contact_book_comment',
        'studentId': str(student_id),
        'studentName': str(student_name),
        'date': str(date),
    }
    
    count = send_to_role(data_service, 'teacher', title, body, data)
    count += send_to_role(data_service, 'admin', title, body, data)
    
    if count > 0:
        print(f'[FCM] Sent comment notification to {count} devices')
    return count



def notify_parents_new_record(data_service, student_id, student_name, date):
    """Notify parents when a teacher creates/updates a contact book entry."""
    # Resolve student name from cache if it looks like an ID
    if not student_name or student_name == student_id:
        student_name = lookup_student_name(student_id)
    title = f'📖 {student_name} 的聯絡簿已更新'
    body = f'{date} 的聯絡簿已由老師填寫，請查看'
    data = {
        'type': 'contact_book_update',
        'studentId': str(student_id),
        'studentName': str(student_name),
        'date': str(date),
    }
    count = send_to_student_parents(data_service, str(student_id), title, body, data,
                                     pref_column='contact_book_notify')
    if count > 0:
        print(f'[FCM] Sent new record notification to {count} parent devices for {student_name}')
    return count


def notify_parents_new_comment(data_service, student_id, student_name, sender_name, content, date=''):
    """Notify parents when a teacher leaves a comment."""
    # Resolve student name from cache if it looks like an ID
    if not student_name or student_name == student_id:
        student_name = lookup_student_name(student_id)
    is_image = (
        isinstance(content, str) and (
            content.startswith('https://firebasestorage.googleapis.com') or
            content.startswith('https://storage.googleapis.com')
        )
    )
    content_preview = '傳了一張照片 📷' if is_image else content[:100]

    title = f'💬 {student_name} 的聯絡簿有新留言'
    body = content_preview
    data = {
        'type': 'contact_book_comment',
        'studentId': str(student_id),
        'studentName': str(student_name),
        'date': str(date),
    }
    count = send_to_student_parents(data_service, str(student_id), title, body, data,
                                     pref_column='contact_book_notify')
    if count > 0:
        print(f'[FCM] Sent teacher comment notification to {count} parent devices for {student_name}')
    return count


def notify_parents_announcement(data_service, news_id, title_text, body_text=''):
    """Notify all parents about a new announcement, respecting announcement_notify preference."""
    title = f'📢 {title_text}'
    body = body_text or '學校有新公告，請前往查看'
    ndata = {
        'type': 'announcement',
        'id': str(news_id),
    }

    conn = data_service.get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT push_token, user_id FROM push_tokens WHERE role = 'parent'"
        ).fetchall()

        # Exclude users who opted out of announcement notifications
        pref_rows = conn.execute(
            "SELECT user_id FROM notification_preferences WHERE announcement_notify = 0"
        ).fetchall()
        opted_out = {r['user_id'] for r in pref_rows}

        tokens = [r['push_token'] for r in rows if r['user_id'] not in opted_out]

        if tokens:
            count = send_to_tokens(tokens, title, body, ndata)
            if count > 0:
                print(f'[FCM] Sent announcement notification to {count} parent devices')
            return count
        return 0
    finally:
        conn.close()


def notify_teachers_status_update(data_service, student_id, student_name, date, new_status):
    """Silent data-only push so teacher web can instantly update contact book status.
    
    No notification popup is shown — this is a background data channel only.
    new_status: 'read' | 'signed'
    """
    # FCM data-only messages must NOT have a notification block in send_to_tokens
    # We pass empty title/body and rely on data payload exclusively
    data = {
        'type': 'contact_book_status',
        'studentId': str(student_id),
        'studentName': str(student_name),
        'date': str(date),
        'status': str(new_status),
    }

    count = send_to_role(data_service, 'teacher', '', '', data)
    count += send_to_role(data_service, 'admin', '', '', data)

    if count > 0:
        print(f'[FCM] Sent status update ({new_status}) for {student_name} to {count} devices')
    return count

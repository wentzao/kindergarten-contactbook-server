"""
FCM Notification Sender Service
Uses Firebase Cloud Messaging HTTP v1 API directly (no firebase-admin SDK needed).
Only requires: requests, google-auth
"""
import os
import json
import sqlite3
import sys
import warnings
import base64
import time
import requests

# Keep Python 3.8 runtime quiet before migration; this warning is informational.
if sys.version_info[:2] == (3, 8):
    warnings.filterwarnings(
        "ignore",
        message=r".*Python version 3\.8 past its end of life.*",
        category=FutureWarning,
        module=r"google\.auth",
    )

from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

try:
    import httpx
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils as crypto_utils
except Exception:
    httpx = None
    serialization = None
    ec = None
    crypto_utils = None
    hashes = None

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
        return 0

    # Split tokens by type
    expo_tokens = [t for t in tokens if t.startswith('ExponentPushToken[')]
    fcm_tokens = [t for t in tokens if not t.startswith('ExponentPushToken[')]

    count = 0
    if expo_tokens:
        count += _send_expo_push(expo_tokens, title, body, data)
    if fcm_tokens:
        count += _send_fcm(fcm_tokens, title, body, data)

    return count


def _row_value(row, key, default=None):
    try:
        value = row[key]
    except Exception:
        return default
    return default if value is None else value


def _row_provider(row):
    provider = str(_row_value(row, 'provider', '') or '').strip().lower()
    if provider:
        return provider
    token = str(_row_value(row, 'push_token', '') or '')
    return 'expo' if token.startswith('ExponentPushToken[') else 'fcm'


def send_to_push_rows(rows, title, body, data=None):
    """Send notifications to push-token rows with provider metadata."""
    if not rows:
        return 0

    expo_tokens = []
    fcm_tokens = []
    apns_rows = []
    for row in rows:
        provider = _row_provider(row)
        token = str(_row_value(row, 'push_token', '') or '')
        if not token:
            continue
        if provider == 'apns':
            apns_rows.append(row)
        elif provider == 'expo' or token.startswith('ExponentPushToken['):
            expo_tokens.append(token)
        else:
            fcm_tokens.append(token)

    count = 0
    if expo_tokens:
        count += _send_expo_push(expo_tokens, title, body, data)
    if fcm_tokens:
        count += _send_fcm(fcm_tokens, title, body, data)
    if apns_rows:
        count += _send_apns(apns_rows, title, body, data)
    return count


_APNS_KEY = None
_APNS_KEY_PATH = None
_APNS_JWT = None
_APNS_JWT_ISSUED_AT = 0


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _apns_environment_for_row(row):
    value = str(_row_value(row, 'environment', '') or os.environ.get('APNS_ENVIRONMENT', 'sandbox')).lower()
    return 'production' if value in ('prod', 'production') else 'sandbox'


def _apns_bundle_for_row(row):
    return (
        str(_row_value(row, 'bundle_id', '') or '').strip()
        or os.environ.get('APNS_BUNDLE_ID', '').strip()
        or 'com.wentzao.WenTzaoConnect'
    )


def _load_apns_key():
    global _APNS_KEY, _APNS_KEY_PATH
    if serialization is None:
        print('[APNs] cryptography/httpx dependencies are not installed')
        return None

    key_path = os.environ.get(
        'APNS_AUTH_KEY_PATH',
        os.path.join(os.path.dirname(os.path.dirname(__file__)), 'apns-auth-key.p8')
    )
    if _APNS_KEY is not None and _APNS_KEY_PATH == key_path:
        return _APNS_KEY
    if not os.path.exists(key_path):
        print(f'[APNs] Auth key not found: {key_path}')
        return None

    with open(key_path, 'rb') as key_file:
        _APNS_KEY = serialization.load_pem_private_key(key_file.read(), password=None)
        _APNS_KEY_PATH = key_path
    return _APNS_KEY


def _apns_jwt():
    global _APNS_JWT, _APNS_JWT_ISSUED_AT
    team_id = os.environ.get('APNS_TEAM_ID', '').strip()
    key_id = os.environ.get('APNS_KEY_ID', '').strip()
    if not team_id or not key_id:
        print('[APNs] APNS_TEAM_ID or APNS_KEY_ID is missing')
        return None

    now = int(time.time())
    if _APNS_JWT and now - _APNS_JWT_ISSUED_AT < 45 * 60:
        return _APNS_JWT

    private_key = _load_apns_key()
    if private_key is None:
        return None

    header = _b64url(json.dumps({'alg': 'ES256', 'kid': key_id}, separators=(',', ':')).encode())
    payload = _b64url(json.dumps({'iss': team_id, 'iat': now}, separators=(',', ':')).encode())
    signing_input = f'{header}.{payload}'.encode()
    der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = crypto_utils.decode_dss_signature(der_signature)
    raw_signature = r.to_bytes(32, byteorder='big') + s.to_bytes(32, byteorder='big')
    _APNS_JWT = f'{header}.{payload}.{_b64url(raw_signature)}'
    _APNS_JWT_ISSUED_AT = now
    return _APNS_JWT


def _send_apns(rows, title, body, data=None):
    """Send notifications via Apple's APNs HTTP/2 API."""
    if httpx is None:
        print('[APNs] httpx dependency is not installed')
        return 0

    token = _apns_jwt()
    if not token:
        return 0

    success_count = 0
    by_environment = {}
    for row in rows:
        env = _apns_environment_for_row(row)
        by_environment.setdefault(env, []).append(row)

    for env, env_rows in by_environment.items():
        host = 'https://api.push.apple.com' if env == 'production' else 'https://api.sandbox.push.apple.com'
        try:
            with httpx.Client(http2=True, timeout=10) as client:
                for row in env_rows:
                    device_token = str(_row_value(row, 'push_token', '') or '').strip()
                    if not device_token:
                        continue
                    alert_payload = {'data': data or {}}
                    if title:
                        alert_payload['aps'] = {
                            'alert': {'title': title, 'body': body or ''},
                            'sound': 'default',
                        }
                    else:
                        alert_payload['aps'] = {'content-available': 1}

                    headers = {
                        'authorization': f'bearer {token}',
                        'apns-topic': _apns_bundle_for_row(row),
                        'apns-push-type': 'alert' if title else 'background',
                        'apns-priority': '10' if title else '5',
                    }
                    resp = client.post(
                        f'{host}/3/device/{device_token}',
                        headers=headers,
                        json=alert_payload,
                    )
                    if 200 <= resp.status_code < 300:
                        success_count += 1
                    else:
                        print(f'[APNs] Send failed ({resp.status_code}): {resp.text[:200]}')
        except Exception as e:
            print(f'[APNs] Request error: {e}')

    return success_count


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
                receipts = resp.json().get('data', {})
                for rid, receipt in receipts.items():
                    if receipt.get('status') == 'error':
                        print(f'[ExpoPush] Receipt error: {receipt}')
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
            '''
            SELECT DISTINCT push_token, provider, platform, environment, bundle_id
            FROM push_tokens
            WHERE role = ?
            ''',
            (role,)
        ).fetchall()
        return send_to_push_rows(rows, title, body, data)
    finally:
        conn.close()


def send_to_teacher_user_ids(data_service, user_ids, title, body, data=None):
    """Send notification to specific teacher/admin user ids."""
    normalized = sorted({str(uid).strip() for uid in user_ids if str(uid).strip()})
    if not normalized:
        return 0

    conn = data_service.get_db()
    try:
        placeholders = ','.join('?' for _ in normalized)
        rows = conn.execute(f'''
            SELECT DISTINCT pt.push_token, pt.provider, pt.platform, pt.environment, pt.bundle_id, pt.user_id
            FROM push_tokens pt
            LEFT JOIN notification_preferences np ON np.user_id = pt.user_id
            WHERE pt.user_id IN ({placeholders})
              AND pt.role IN ('teacher', 'admin')
              AND COALESCE(np.contact_book_notify, 1) = 1
        ''', normalized).fetchall()
        return send_to_push_rows(rows, title, body, data)
    finally:
        conn.close()


def resolve_teacher_user_ids_for_student(data_service, student_id, semester=None, class_name=None):
    """Resolve teacher userIds responsible for a student's class."""
    conn = data_service.get_db()
    try:
        resolved_class = (class_name or '').strip()
        resolved_semester = (semester or '').strip()
        if not resolved_class:
            row = conn.execute('''
                SELECT class_name, semester
                FROM student_class_cache
                WHERE student_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
            ''', (str(student_id),)).fetchone()
            if row:
                resolved_class = row['class_name'] or ''
                resolved_semester = resolved_semester or (row['semester'] or '')

        if not resolved_class:
            print(f'[PushScope] No class mapping found for student {student_id}')
            return []

        params = [resolved_class]
        sql = 'SELECT DISTINCT user_id FROM teacher_class_memberships WHERE class_name = ?'
        if resolved_semester:
            sql += ' AND semester = ?'
            params.append(resolved_semester)
        rows = conn.execute(sql, params).fetchall()
        return [r['user_id'] for r in rows]
    except Exception as e:
        print(f'[PushScope] Resolve error for student {student_id}: {e}')
        return []
    finally:
        conn.close()


def _send_to_responsible_teachers(data_service, student_id, title, body, data=None, semester=None, class_name=None):
    user_ids = resolve_teacher_user_ids_for_student(data_service, student_id, semester=semester, class_name=class_name)
    if not user_ids:
        if os.environ.get('NOTIFICATION_FALLBACK_BROADCAST', '').strip().lower() in ('1', 'true', 'yes'):
            count = send_to_role(data_service, 'teacher', title, body, data)
            count += send_to_role(data_service, 'admin', title, body, data)
            return count
        return 0
    return send_to_teacher_user_ids(data_service, user_ids, title, body, data)


def send_to_student_parents(data_service, student_id, title, body, data=None, pref_column=None):
    """Send notification to all parent devices that are linked to a specific student.

    If pref_column is provided (e.g. 'contact_book_notify'), users who have opted out
    of that notification type will be excluded.
    """
    conn = data_service.get_db()
    try:
        rows = conn.execute(
            '''
            SELECT DISTINCT push_token, user_id, student_ids, provider, platform, environment, bundle_id
            FROM push_tokens
            WHERE role = 'parent' AND student_ids IS NOT NULL
            ''',
        ).fetchall()

        # Build set of opted-out user_ids
        _VALID_PREF_COLUMNS = {'contact_book_notify', 'announcement_notify'}
        opted_out = set()
        if pref_column and pref_column in _VALID_PREF_COLUMNS:
            pref_rows = conn.execute(
                f"SELECT user_id FROM notification_preferences WHERE {pref_column} = 0"
            ).fetchall()
            opted_out = {r['user_id'] for r in pref_rows}

        target_rows = []
        for r in rows:
            try:
                sids = json.loads(r['student_ids']) if r['student_ids'] else []
                if student_id in sids:
                    if pref_column and r['user_id'] in opted_out:
                        continue
                    target_rows.append(r)
            except (json.JSONDecodeError, TypeError):
                continue
        return send_to_push_rows(target_rows, title, body, data)
    finally:
        conn.close()


def notify_teachers_new_comment(data_service, student_id, student_name, sender_name, content, date='', class_name=None):
    """Notify responsible teachers when a parent leaves a comment."""
    # If content is a Firebase Storage URL, show a friendly label instead of the raw URL
    is_image = (
        isinstance(content, str) and (
            content.startswith('https://firebasestorage.googleapis.com') or
            content.startswith('https://storage.googleapis.com') or
            content.startswith('https://imageserver.wentzao.com')
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
    
    count = _send_to_responsible_teachers(
        data_service,
        student_id,
        title,
        body,
        data,
        class_name=class_name,
    )
    
    if count > 0:
        print(f'[Push] Sent comment notification to {count} responsible teacher devices')
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
            content.startswith('https://storage.googleapis.com') or
            content.startswith('https://imageserver.wentzao.com')
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
            '''
            SELECT DISTINCT push_token, user_id, provider, platform, environment, bundle_id
            FROM push_tokens
            WHERE role = 'parent'
            '''
        ).fetchall()

        # Exclude users who opted out of announcement notifications
        pref_rows = conn.execute(
            "SELECT user_id FROM notification_preferences WHERE announcement_notify = 0"
        ).fetchall()
        opted_out = {r['user_id'] for r in pref_rows}

        target_rows = [r for r in rows if r['user_id'] not in opted_out]
        count = send_to_push_rows(target_rows, title, body, ndata)
        if count > 0:
            print(f'[Push] Sent announcement notification to {count} parent devices')
        return count
    finally:
        conn.close()


def notify_parents_announcement_update(data_service, news_id):
    """Silent data-only push so the app can refresh its announcement cache.

    No notification banner is shown — this is a background cache-invalidation signal.
    Respects the announcement_notify preference so opted-out users are still excluded.
    """
    ndata = {
        'type': 'announcement_update',
        'id': str(news_id),
    }

    conn = data_service.get_db()
    try:
        rows = conn.execute(
            '''
            SELECT DISTINCT push_token, user_id, provider, platform, environment, bundle_id
            FROM push_tokens
            WHERE role = 'parent'
            '''
        ).fetchall()

        pref_rows = conn.execute(
            "SELECT user_id FROM notification_preferences WHERE announcement_notify = 0"
        ).fetchall()
        opted_out = {r['user_id'] for r in pref_rows}

        target_rows = [r for r in rows if r['user_id'] not in opted_out]
        count = send_to_push_rows(target_rows, '', '', ndata)
        if count > 0:
            print(f'[Push] Sent silent update notification to {count} parent devices')
        return count
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

    count = _send_to_responsible_teachers(data_service, student_id, '', '', data)

    if count > 0:
        print(f'[Push] Sent status update ({new_status}) for {student_name} to {count} responsible teacher devices')
    return count


def notify_teachers_comment_deleted(data_service, student_id, date, content='', sender_name='家長', class_name=None):
    """Visible push notification to teachers/admins when a parent deletes a chat message.

    Detects whether the deleted item was a photo or a text message and produces
    the appropriate title and body. sender_name is the parent's display name.
    The imageUrl data field is kept for cache-eviction purposes on the web client.
    """
    student_name = lookup_student_name(student_id)

    is_image = (
        isinstance(content, str) and (
            content.startswith('https://firebasestorage.googleapis.com') or
            content.startswith('https://storage.googleapis.com') or
            content.startswith('https://imageserver.wentzao.com')
        )
    )

    if is_image:
        title = '照片已刪除'
        body = f'{student_name} 的家長刪除了 {date} 的一張照片'
    else:
        title = '留言已刪除'
        body = f'{student_name} 的家長刪除了 {date} 的一則留言'

    data = {
        'type': 'contact_book_comment_deleted',
        'studentId': str(student_id),
        'date': str(date),
        'imageUrl': content if is_image else '',
        'senderName': str(sender_name),
    }
    count = _send_to_responsible_teachers(
        data_service,
        student_id,
        title,
        body,
        data,
        class_name=class_name,
    )
    if count > 0:
        print(f'[Push] Sent comment-deleted notification to {count} responsible teacher devices')
    return count


def notify_parents_comment_deleted(data_service, student_id, date, image_url=''):
    """Push to parents when a teacher deletes a chat photo.

    Must include a title/body so Expo Push reliably delivers it via
    addNotificationReceivedListener on iOS. The App suppresses the visible
    banner for this notification type via setNotificationHandler.
    """
    data = {
        'type': 'contact_book_comment_deleted',
        'studentId': str(student_id),
        'date': str(date),
        'imageUrl': str(image_url),
        'senderRole': 'teacher',
    }
    count = send_to_student_parents(data_service, str(student_id),
                                    '照片已移除', '老師刪除了一張聊天照片', data,
                                    pref_column='contact_book_notify')
    if count > 0:
        print(f'[FCM] Sent teacher comment-deleted to {count} parent devices')
    return count

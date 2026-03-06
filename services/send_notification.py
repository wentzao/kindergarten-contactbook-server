"""
FCM Notification Sender Service
Uses Firebase Cloud Messaging HTTP v1 API directly (no firebase-admin SDK needed).
Only requires: requests, google-auth
"""
import os
import json
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

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
    Send a push notification to a list of FCM tokens.
    Returns the number of successfully sent messages.
    """
    if not _init() or not tokens:
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
        payload = {
            'message': {
                'token': token,
                'notification': {
                    'title': title,
                    'body': body,
                },
                'data': data or {},
            }
        }
        
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


def notify_teachers_new_comment(data_service, student_id, student_name, sender_name, content, date=''):
    """Notify all teachers and admins when a parent leaves a comment."""
    title = f'💬 {student_name} 的聯絡簿有新留言'
    body = f'{sender_name}: {content[:100]}'
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

def notify_teachers_status_update(data_service, student_id, student_name, status, date=''):
    """Notify all teachers and admins when a contact book status changes (e.g., read, signed)."""
    # Use a silent data message to update UI without showing an alert to teachers
    # unless we want to show a toast, but typically real-time UI updates don't need noisy alerts.
    # However, FCM display notifications require title/body to trigger on system tray if app is backgrounded.
    # If app is foregrounded, onForegroundMessage catches it.
    
    status_label = '已讀' if status == 'read' else '已簽名' if status == 'signed' else status
    title = f'✅ {student_name} 的聯絡簿{status_label}'
    body = f'家長已將 {student_name} 的聯絡簿標記為{status_label}'
    
    data = {
        'type': 'contact_book_status',
        'studentId': str(student_id),
        'studentName': str(student_name),
        'status': str(status),
        'date': str(date),
        'silent': 'true' # A hint for the frontend to maybe not show a full toast
    }
    
    count = send_to_role(data_service, 'teacher', title, body, data)
    count += send_to_role(data_service, 'admin', title, body, data)
    
    if count > 0:
        print(f'[FCM] Sent status update notification to {count} devices')
    return count


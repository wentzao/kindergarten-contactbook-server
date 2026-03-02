from flask import Blueprint, request, jsonify
from datetime import datetime
from services.data_service import DataService
import os

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
        ''')
        # Migration: add role column if it doesn't exist
        try:
            conn.execute('ALTER TABLE push_tokens ADD COLUMN role VARCHAR(20) DEFAULT \'parent\'')
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
    """Register or update a FCM push token for a user."""
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    user_id = data.get('userId')
    push_token = data.get('pushToken')

    if not user_id or not push_token:
        return jsonify({'error': 'userId and pushToken are required'}), 400

    device_name = data.get('deviceName', 'Unknown')
    role = data.get('role', 'parent')  # 'parent', 'teacher', or 'admin'
    now = datetime.now().isoformat()

    conn = data_service.get_db()
    try:
        # UPSERT: insert or update
        conn.execute('''
            INSERT INTO push_tokens (user_id, push_token, device_name, role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, push_token) DO UPDATE SET
                device_name = excluded.device_name,
                role = excluded.role,
                updated_at = excluded.updated_at
        ''', (user_id, push_token, device_name, role, now, now))
        conn.commit()
        return jsonify({'status': 'ok', 'message': 'Push token registered'}), 200
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

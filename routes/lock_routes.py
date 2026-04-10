"""
Editing lock routes — lightweight pessimistic lock + FCM silent notifications.

Lock types:
  - journal:{className}:{date}   — class journal content blocks
  - note:{studentId}:{date}      — individual student note

Locks auto-expire after LOCK_TTL_MINUTES. Frontend sends heartbeats to keep alive.
"""
from flask import Blueprint, request, jsonify
import os, json, threading
from datetime import datetime, timedelta

lock_bp = Blueprint('locks', __name__)

from services.data_service import DataService
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

LOCK_TTL_MINUTES = 3


# ── Ensure table ──
def ensure_lock_table():
    conn = data_service.get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS editing_locks (
                lock_key VARCHAR(200) PRIMARY KEY,
                locked_by VARCHAR(100) NOT NULL,
                lock_owner_id VARCHAR(150),
                locked_by_name VARCHAR(100),
                locked_at VARCHAR(50) NOT NULL,
                expires_at VARCHAR(50) NOT NULL
            );
        ''')
        columns = [row[1] for row in conn.execute('PRAGMA table_info(editing_locks)').fetchall()]
        if 'lock_owner_id' not in columns:
            conn.execute('ALTER TABLE editing_locks ADD COLUMN lock_owner_id VARCHAR(150)')
        conn.execute('UPDATE editing_locks SET lock_owner_id = locked_by WHERE lock_owner_id IS NULL OR lock_owner_id = ""')
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

ensure_lock_table()


def _now():
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')


def _expires():
    return (datetime.now() + timedelta(minutes=LOCK_TTL_MINUTES)).strftime('%Y-%m-%dT%H:%M:%S+08:00')


def _is_expired(expires_at_str):
    try:
        exp = datetime.fromisoformat(expires_at_str.replace('+08:00', ''))
        return datetime.now() > exp
    except Exception:
        return True


def _send_lock_notification(lock_type, lock_key, locked_by_name, lock_owner_id='', exclude_user=None):
    """Send silent FCM to all teachers/admins about lock state change."""
    def _send():
        try:
            from services.send_notification import send_to_role
            data = {
                'type': lock_type,        # 'lock_acquired' | 'lock_released'
                'lockKey': lock_key,
                'lockedBy': locked_by_name or '',
                'lockOwnerId': lock_owner_id or '',
            }
            send_to_role(data_service, 'teacher', '', '', data)
            send_to_role(data_service, 'admin', '', '', data)
        except Exception as e:
            print(f'[Lock] FCM notification error: {e}')

    threading.Thread(target=_send, daemon=True).start()


# ── Clean expired locks ──
def _clean_expired():
    conn = data_service.get_db()
    try:
        now = _now()
        conn.execute('DELETE FROM editing_locks WHERE expires_at < ?', (now,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


# ── Acquire lock ──
@lock_bp.route('/acquire', methods=['POST'])
def acquire_lock():
    data = request.get_json() or {}
    lock_key = data.get('lockKey')
    user_id = data.get('userId')
    user_name = data.get('userName', '')
    lock_owner_id = data.get('lockOwnerId') or user_id

    if not lock_key or not user_id:
        return jsonify({'error': 'lockKey and userId required'}), 400

    _clean_expired()

    conn = data_service.get_db()
    try:
        existing = conn.execute(
            'SELECT * FROM editing_locks WHERE lock_key = ?', (lock_key,)
        ).fetchone()

        existing_owner_id = existing['lock_owner_id'] if existing and 'lock_owner_id' in existing.keys() else None

        if existing and (existing_owner_id or existing['locked_by']) != lock_owner_id:
            # Locked by another device/window owner
            return jsonify({
                'acquired': False,
                'lockedBy': existing['locked_by'],
                'lockOwnerId': existing_owner_id or existing['locked_by'],
                'lockedByName': existing['locked_by_name'],
                'expiresAt': existing['expires_at'],
            })

        # Insert or update (same device/window re-acquiring is fine)
        now = _now()
        expires = _expires()
        conn.execute('''
            INSERT INTO editing_locks (lock_key, locked_by, lock_owner_id, locked_by_name, locked_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(lock_key) DO UPDATE SET
                locked_by = excluded.locked_by,
                lock_owner_id = excluded.lock_owner_id,
                locked_by_name = excluded.locked_by_name,
                locked_at = excluded.locked_at,
                expires_at = excluded.expires_at
        ''', (lock_key, user_id, lock_owner_id, user_name, now, expires))
        conn.commit()

        _send_lock_notification('lock_acquired', lock_key, user_name, lock_owner_id, exclude_user=user_id)

        return jsonify({
            'acquired': True,
            'lockOwnerId': lock_owner_id,
            'expiresAt': expires,
        })
    finally:
        conn.close()


# ── Release lock ──
@lock_bp.route('/release', methods=['POST'])
def release_lock():
    data = request.get_json() or {}
    lock_key = data.get('lockKey')
    user_id = data.get('userId')
    lock_owner_id = data.get('lockOwnerId') or user_id

    if not lock_key or not user_id:
        return jsonify({'error': 'lockKey and userId required'}), 400

    conn = data_service.get_db()
    try:
        existing = conn.execute(
            'SELECT * FROM editing_locks WHERE lock_key = ?', (lock_key,)
        ).fetchone()

        existing_owner_id = existing['lock_owner_id'] if existing and 'lock_owner_id' in existing.keys() else None

        if existing and (existing_owner_id or existing['locked_by']) == lock_owner_id:
            user_name = existing['locked_by_name']
            conn.execute('DELETE FROM editing_locks WHERE lock_key = ?', (lock_key,))
            conn.commit()
            _send_lock_notification('lock_released', lock_key, user_name or '', lock_owner_id)

        return jsonify({'released': True})
    finally:
        conn.close()


# ── Heartbeat (extend expiry) ──
@lock_bp.route('/heartbeat', methods=['POST'])
def heartbeat_lock():
    data = request.get_json() or {}
    lock_key = data.get('lockKey')
    user_id = data.get('userId')
    lock_owner_id = data.get('lockOwnerId') or user_id

    if not lock_key or not user_id:
        return jsonify({'error': 'lockKey and userId required'}), 400

    conn = data_service.get_db()
    try:
        expires = _expires()
        cursor = conn.execute(
            'UPDATE editing_locks SET expires_at = ? WHERE lock_key = ? AND COALESCE(lock_owner_id, locked_by) = ?',
            (expires, lock_key, lock_owner_id)
        )
        conn.commit()
        return jsonify({'renewed': cursor.rowcount > 0, 'expiresAt': expires})
    finally:
        conn.close()


# ── Batch release (multiple locks at once, for beforeunload) ──
@lock_bp.route('/release-batch', methods=['POST'])
def release_batch():
    data = request.get_json() or {}
    lock_keys = data.get('lockKeys', [])
    user_id = data.get('userId')
    lock_owner_id = data.get('lockOwnerId') or user_id

    if not user_id:
        return jsonify({'error': 'userId required'}), 400

    conn = data_service.get_db()
    try:
        released = []
        for key in lock_keys:
            existing = conn.execute(
                'SELECT locked_by_name FROM editing_locks WHERE lock_key = ? AND COALESCE(lock_owner_id, locked_by) = ?',
                (key, lock_owner_id)
            ).fetchone()
            if existing:
                conn.execute(
                    'DELETE FROM editing_locks WHERE lock_key = ? AND COALESCE(lock_owner_id, locked_by) = ?',
                    (key, lock_owner_id)
                )
                released.append(key)

        conn.commit()

        # Send one notification per released lock
        for key in released:
            _send_lock_notification('lock_released', key, '', lock_owner_id)

        return jsonify({'released': released})
    finally:
        conn.close()


# ── Get lock status for a class+date (all locks) ──
@lock_bp.route('/status', methods=['GET'])
def get_lock_status():
    class_name = request.args.get('className')
    date = request.args.get('date')

    if not class_name or not date:
        return jsonify({'error': 'className and date required'}), 400

    _clean_expired()

    conn = data_service.get_db()
    try:
        # Match journal:{className}:{date} and note:*:{date} patterns
        journal_key = f'journal:{class_name}:{date}'
        note_prefix = f'note:%:{date}'

        rows = conn.execute('''
            SELECT * FROM editing_locks
            WHERE lock_key = ? OR lock_key LIKE ?
        ''', (journal_key, note_prefix)).fetchall()

        locks = {}
        for r in rows:
            locks[r['lock_key']] = {
                'lockedBy': r['locked_by'],
                'lockOwnerId': r['lock_owner_id'] or r['locked_by'],
                'lockedByName': r['locked_by_name'],
                'lockedAt': r['locked_at'],
                'expiresAt': r['expires_at'],
            }

        return jsonify({'locks': locks})
    finally:
        conn.close()

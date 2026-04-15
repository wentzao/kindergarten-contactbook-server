from flask import Blueprint, request, jsonify
import os, json, threading
from datetime import datetime

journal_bp = Blueprint('journal', __name__)

# ── Shared DataService instance ──
from services.data_service import DataService
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)


@journal_bp.route('/<class_name>/<date>', methods=['GET'])
def get_journal(class_name, date):
    """Get class journal for a specific class and date (teacher use)."""
    journal = data_service.get_class_journal(class_name, date)
    if journal:
        return jsonify(journal)
    return jsonify({
        'className': class_name,
        'date': date,
        'contentBlocks': [],
        'editedBy': None,
        'notifiedAt': None,
        'updatedAt': None,
    })


@journal_bp.route('/<class_name>/<date>', methods=['PUT'])
def save_journal(class_name, date):
    """Save/update class journal content blocks (auto-save).
    Supports optimistic locking: if lastUpdatedAt is provided, rejects save if server version is newer.
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    content_blocks = data.get('contentBlocks', [])
    edited_by = data.get('editedBy')
    last_updated_at = data.get('lastUpdatedAt')

    # Optimistic lock: check if someone else saved a newer version
    if last_updated_at:
        existing = data_service.get_class_journal(class_name, date)
        if existing and existing.get('updatedAt') and existing['updatedAt'] != last_updated_at:
            return jsonify({
                'error': 'conflict',
                'message': '此日誌已被其他老師更新',
                'serverUpdatedAt': existing['updatedAt'],
                'editedBy': existing.get('editedBy'),
            }), 409

    result = data_service.save_class_journal(
        class_name, date, content_blocks, edited_by
    )

    # Send silent "data_updated" notification to other teachers
    def _notify_update():
        try:
            from services.send_notification import send_to_role
            notify_data = {
                'type': 'data_updated',
                'dataType': 'class_journal',
                'className': class_name,
                'date': date,
                'updatedAt': result.get('updatedAt', ''),
            }
            send_to_role(data_service, 'teacher', '', '', notify_data)
            send_to_role(data_service, 'admin', '', '', notify_data)
        except Exception as e:
            print(f'[Journal] data_updated notification error: {e}')

    threading.Thread(target=_notify_update, daemon=True).start()

    return jsonify(result)


@journal_bp.route('/<class_name>/<date>', methods=['DELETE'])
def delete_journal(class_name, date):
    """Delete class journal."""
    deleted = data_service.delete_class_journal(class_name, date)
    if deleted:
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Journal not found'}), 404


@journal_bp.route('/<class_name>/<date>/publish', methods=['POST'])
def publish_journal(class_name, date):
    """Publish class journal: set notified_at, update contact_books status, send push notifications."""
    data = request.get_json() or {}
    student_ids = data.get('studentIds', [])
    student_names = data.get('studentNames', {})
    sent_by = data.get('sentBy')

    # 1. Mark class journal as published
    data_service.publish_class_journal(class_name, date)

    # 2. Batch UPSERT contact_books: create if missing, upgrade draft → notified
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
    year, month = int(date.split('-')[0]), int(date.split('-')[1])
    conn = data_service.get_db()
    try:
        for sid in student_ids:
            row = conn.execute(
                'SELECT id, status FROM contact_books WHERE student_id = ? AND date = ?',
                (sid, date)
            ).fetchone()
            if not row:
                # No record yet — create a minimal notified entry
                conn.execute('''
                    INSERT INTO contact_books (student_id, date, year, month, status, notified_at)
                    VALUES (?, ?, ?, ?, 'notified', ?)
                ''', (sid, date, year, month, now))
            elif row['status'] == 'draft':
                conn.execute(
                    'UPDATE contact_books SET status = ?, notified_at = ? WHERE id = ?',
                    ('notified', now, row['id'])
                )

        # Record notification log
        conn.execute('''
            INSERT INTO notification_logs (class_name, date, student_count, student_ids, sent_by, sent_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (class_name, date, len(student_ids), json.dumps(student_ids), sent_by, now))
        conn.commit()
    finally:
        conn.close()

    # 3. Send push notifications in background
    notified_count = 0
    if student_ids:
        def _send_notifications():
            from services.send_notification import notify_parents_new_record
            for sid in student_ids:
                s_name = student_names.get(sid, sid)
                try:
                    notify_parents_new_record(data_service, sid, s_name, date)
                except Exception as e:
                    print(f"[publish] notify error for {sid}: {e}")

        threading.Thread(target=_send_notifications, daemon=True).start()
        notified_count = len(student_ids)

    return jsonify({'published': True, 'notifiedCount': notified_count, 'sentAt': now})


@journal_bp.route('/student/<student_id>/<date>', methods=['GET'])
def get_journal_for_student(student_id, date):
    """Parent-facing: get class journal for a student on a date.
    Only returns data if the STUDENT has been notified (per-student contact_books.status).
    Requires ?className= query param."""
    class_name = request.args.get('className')
    if not class_name:
        return jsonify({'error': 'className query param required'}), 400

    # Check per-student notification status (not class-level)
    conn = data_service.get_db()
    try:
        row = conn.execute(
            'SELECT status FROM contact_books WHERE student_id = ? AND date = ?',
            (student_id, date)
        ).fetchone()
    finally:
        conn.close()
    student_notified = row and row['status'] in ('notified', 'read', 'signed')

    journal = data_service.get_class_journal(class_name, date)
    if not journal or not student_notified:
        return jsonify({
            'date': date,
            'classJournal': None,
        })

    return jsonify({
        'date': date,
        'classJournal': {
            'semester': journal.get('semester'),
            'contentBlocks': journal.get('contentBlocks', []),
            'editedBy': journal.get('editedBy'),
            'updatedAt': journal.get('updatedAt'),
        },
    })

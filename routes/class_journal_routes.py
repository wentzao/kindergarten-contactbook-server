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
    """Save/update class journal content blocks (auto-save)."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    content_blocks = data.get('contentBlocks', [])
    edited_by = data.get('editedBy')

    result = data_service.save_class_journal(
        class_name, date, content_blocks, edited_by
    )
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

    # 2. Batch update contact_books: draft → notified + record log
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
    conn = data_service.get_db()
    try:
        if student_ids:
            placeholders = ','.join(['?'] * len(student_ids))
            conn.execute(f'''
                UPDATE contact_books SET status = 'notified', notified_at = ?
                WHERE student_id IN ({placeholders}) AND date = ? AND status = 'draft'
            ''', (now, *student_ids, date))

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
    Only returns data if journal has been published (notified_at IS NOT NULL).
    Requires ?className= query param."""
    class_name = request.args.get('className')
    if not class_name:
        return jsonify({'error': 'className query param required'}), 400

    journal = data_service.get_class_journal(class_name, date)
    if not journal or not journal.get('notifiedAt'):
        return jsonify({
            'date': date,
            'classJournal': None,
        })

    return jsonify({
        'date': date,
        'classJournal': {
            'contentBlocks': journal.get('contentBlocks', []),
            'updatedAt': journal.get('updatedAt'),
        },
    })

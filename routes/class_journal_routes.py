from flask import Blueprint, request, jsonify
import os, json
from datetime import datetime

journal_bp = Blueprint('journal', __name__)

# ── Shared DataService instance ──
from services.data_service import DataService
from services.contact_book_publish_service import (
    ensure_scheduled_contact_book_notifications_table,
    publish_contact_book_in_transaction,
)
from services.push_outbox_service import EVENT_ROLE_PUSH, enqueue_push_job, ensure_push_outbox_table
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

STATUS_RANK = {
    'draft': 0,
    'notified': 1,
    'read': 2,
    'signed': 3,
}


def _canonical_contact_book_status(row):
    if not row:
        return 'draft'

    inferred = 'draft'
    if row['signed_at']:
        inferred = 'signed'
    elif row['read_at']:
        inferred = 'read'
    elif row['notified_at']:
        inferred = 'notified'

    explicit = (row['status'] or '').strip().lower()
    if explicit not in STATUS_RANK:
        return inferred
    return explicit if STATUS_RANK[explicit] >= STATUS_RANK[inferred] else inferred


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

    # Send silent "data_updated" notification to other teachers via durable outbox.
    notify_conn = data_service.get_db()
    try:
        ensure_push_outbox_table(notify_conn)
        enqueue_push_job(
            notify_conn,
            EVENT_ROLE_PUSH,
            'roles',
            recipient_id='teacher,admin',
            payload={
                'roles': ['teacher', 'admin'],
                'title': '',
                'body': '',
                'data': {
                    'type': 'data_updated',
                    'dataType': 'class_journal',
                    'className': class_name,
                    'date': date,
                    'updatedAt': result.get('updatedAt', ''),
                },
            },
            idempotency_key=f'class_journal_data_updated:{class_name}:{date}:{result.get("updatedAt", "")}',
        )
        notify_conn.commit()
    except Exception as e:
        notify_conn.rollback()
        print(f'[Journal] data_updated outbox error: {e}')
    finally:
        notify_conn.close()

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

    # Mark the journal, contact books, notification log, and outbox jobs together.
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
    notified_count = 0
    conn = data_service.get_db()
    try:
        ensure_scheduled_contact_book_notifications_table(conn)
        conn.execute(
            'UPDATE class_journals SET notified_at = ? WHERE class_name = ? AND date = ?',
            (now, class_name, date),
        )
        for sid in student_ids:
            publish_result = publish_contact_book_in_transaction(
                conn,
                sid,
                date,
                class_name=class_name,
                student_name=student_names.get(sid, sid),
                sent_by=sent_by or '',
                mode='class_journal',
            )
            if not publish_result['alreadyPublished']:
                notified_count += 1

        # Record notification log
        conn.execute('''
            INSERT INTO notification_logs (class_name, date, student_count, student_ids, sent_by, sent_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (class_name, date, len(student_ids), json.dumps(student_ids), sent_by, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return jsonify({'published': True, 'notifiedCount': notified_count, 'deliveryQueued': notified_count, 'sentAt': now})


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
            'SELECT status, notified_at, read_at, signed_at FROM contact_books WHERE student_id = ? AND date = ?',
            (student_id, date)
        ).fetchone()
    finally:
        conn.close()
    student_notified = _canonical_contact_book_status(row) in ('notified', 'read', 'signed')

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

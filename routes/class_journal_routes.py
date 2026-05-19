"""Class journal routes — content only.

Visibility/notify is owned by class_notification_grants (Schema v2).
This module no longer touches contact_books.status or class_journals.notified_at.
"""
from flask import Blueprint, request, jsonify
import os
import json
from datetime import datetime

from services.data_service import DataService
from services import notification_grant_service as grant_service
from services.push_outbox_service import EVENT_ROLE_PUSH, enqueue_push_job, ensure_push_outbox_table


journal_bp = Blueprint('journal', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)


@journal_bp.route('/<class_name>/<date>', methods=['GET'])
def get_journal(class_name, date):
    """Get class journal for a specific class and date (teacher use)."""
    journal = data_service.get_class_journal(class_name, date)
    if journal:
        # Drop legacy notifiedAt (no longer authoritative)
        journal.pop('notified_at', None)
        journal.pop('notifiedAt', None)
        return jsonify(journal)
    return jsonify({
        'className': class_name,
        'date': date,
        'contentBlocks': [],
        'editedBy': None,
        'updatedAt': None,
    })


@journal_bp.route('/<class_name>/<date>', methods=['PUT'])
def save_journal(class_name, date):
    """Save/update class journal content blocks (auto-save).
    Supports optimistic locking: if lastUpdatedAt is provided, rejects save
    if server version is newer."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    content_blocks = data.get('contentBlocks', [])
    edited_by = data.get('editedBy')
    last_updated_at = data.get('lastUpdatedAt')

    if last_updated_at:
        existing = data_service.get_class_journal(class_name, date)
        if existing and existing.get('updatedAt') and existing['updatedAt'] != last_updated_at:
            return jsonify({
                'error': 'conflict',
                'message': '此日誌已被其他老師更新',
                'serverUpdatedAt': existing['updatedAt'],
                'editedBy': existing.get('editedBy'),
            }), 409

    result = data_service.save_class_journal(class_name, date, content_blocks, edited_by)

    # Silently notify other teachers that data updated
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
    deleted = data_service.delete_class_journal(class_name, date)
    if deleted:
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Journal not found'}), 404


@journal_bp.route('/<class_name>/<date>/publish', methods=['POST'])
def publish_journal(class_name, date):
    """LEGACY: publish journal == grant parent visibility.
    Delegates to notification_grant_service. New clients should call
    POST /api/notifications/grants/<class>/<date> directly."""
    data = request.get_json() or {}
    student_ids = data.get('studentIds', [])
    student_names = data.get('studentNames', {})
    sent_by = data.get('sentBy') or ''

    if not student_ids:
        return jsonify({'error': 'studentIds required'}), 400

    try:
        result = grant_service.grant_now(
            data_service, class_name, date, student_ids,
            sent_by=sent_by, student_names=student_names,
            mode='journal_publish',
        )
        return jsonify({
            'published': True,
            'notifiedCount': len(result.get('pushedStudentIds', [])),
            'deliveryQueued': len(result.get('pushedStudentIds', [])),
            'sentAt': result['grant']['notifiedAt'] if result.get('grant') else None,
            'grant': result.get('grant'),
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@journal_bp.route('/student/<student_id>/<date>', methods=['GET'])
def get_journal_for_student(student_id, date):
    """Parent-facing: get class journal for a student on a date.
    Returns content only if (class_name, date) is granted (and the student
    is in the grant's student_ids).
    Requires ?className= query param."""
    class_name = request.args.get('className')
    if not class_name:
        return jsonify({'error': 'className query param required'}), 400

    conn = data_service.get_db()
    try:
        grant = grant_service.get_grant(conn, class_name, date)
        visible = grant_service.is_visible_to_student(grant, student_id)
    finally:
        conn.close()

    journal = data_service.get_class_journal(class_name, date)
    if not journal or not visible:
        return jsonify({'date': date, 'classJournal': None})

    return jsonify({
        'date': date,
        'classJournal': {
            'semester': journal.get('semester'),
            'contentBlocks': journal.get('contentBlocks', []),
            'editedBy': journal.get('editedBy'),
            'updatedAt': journal.get('updatedAt'),
        },
    })

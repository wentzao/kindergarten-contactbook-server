"""
Realtime collaboration routes.

Phase 1 intentionally reuses the existing tables:
  - journal:{className}:{date} is persisted to class_journals.content_blocks

Presence and connected sessions are in memory. With the current deployment
(`gunicorn -k eventlet -w 1`) this gives a practical live editing layer without
adding schema. If the deployment later uses multiple workers, this module needs
a shared pub/sub backend.
"""
from flask import Blueprint, jsonify, request
from flask_sock import Sock
import json
import os
import threading
import time
import uuid
from datetime import datetime

from services.data_service import DataService


collab_bp = Blueprint('collab', __name__)
collab_sock = Sock()

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

SAVE_DEBOUNCE_SECONDS = 1.0
STALE_PARTICIPANT_SECONDS = 45
MAX_TEXT_HISTORY_PER_BLOCK = 500

_state_lock = threading.RLock()
_documents = {}


def _now():
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')


def _default_actor():
    return {
        'actorId': '',
        'displayName': '老師',
        'deviceId': '',
        'color': '#2f80ed',
    }


def _parse_document_id(document_id):
    if not document_id:
        return None
    if document_id.startswith('journal:'):
        rest = document_id[len('journal:'):]
        if ':' not in rest:
            return None
        class_name, date = rest.rsplit(':', 1)
        if class_name and date:
            return {'kind': 'journal', 'className': class_name, 'date': date}
    if document_id.startswith('note:'):
        rest = document_id[len('note:'):]
        if ':' not in rest:
            return None
        student_id, date = rest.rsplit(':', 1)
        if student_id and date:
            return {'kind': 'note', 'studentId': student_id, 'date': date}
    return None


def _load_json(value, fallback=None):
    if value is None or value == '':
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _contact_row_to_note_snapshot(row, parsed):
    teacher_data = _load_json(row['original_teacher'] if row else None, {}) or {}
    items = _load_json(row['items_to_bring'] if row else None)
    if items and isinstance(items, dict) and 'items' in items:
        teacher_data['itemsToBring'] = items.get('items') or []
    returned = _load_json(row['returned_items'] if row else None)
    if returned:
        teacher_data['returnedItems'] = returned
    if row and row['survey_id']:
        teacher_data['surveyId'] = row['survey_id']

    return {
        'documentId': f"note:{parsed['studentId']}:{parsed['date']}",
        'kind': 'note',
        'studentId': parsed['studentId'],
        'date': parsed['date'],
        'note': teacher_data,
        'updatedAt': row['last_modified'] if row else None,
        'editedBy': _load_json(row['edited_by'] if row else None),
    }


def _load_snapshot(document_id):
    parsed = _parse_document_id(document_id)
    if not parsed:
        return None

    if parsed['kind'] == 'journal':
        record = data_service.get_class_journal(parsed['className'], parsed['date'])
        return {
            'documentId': document_id,
            'kind': 'journal',
            'className': parsed['className'],
            'date': parsed['date'],
            'contentBlocks': record.get('contentBlocks', []) if record else [],
            'updatedAt': record.get('updatedAt') if record else None,
            'editedBy': record.get('editedBy') if record else None,
        }

    if parsed['kind'] == 'note':
        conn = data_service.get_db()
        try:
            row = conn.execute(
                '''SELECT original_teacher, items_to_bring, returned_items, survey_id, edited_by, last_modified
                   FROM contact_books WHERE student_id = ? AND date = ?''',
                (parsed['studentId'], parsed['date'])
            ).fetchone()
            return _contact_row_to_note_snapshot(row, parsed)
        finally:
            conn.close()

    return None


def _ensure_document(document_id):
    with _state_lock:
        doc = _documents.get(document_id)
        if doc:
            return doc

        snapshot = _load_snapshot(document_id)
        if snapshot is None:
            return None

        doc = {
            'documentId': document_id,
            'snapshot': snapshot,
            'serverSeq': 0,
            'blockVersions': {},
            'textHistory': {},
            'clients': {},
            'participants': {},
            'saveTimer': None,
            'dirty': False,
        }
        _documents[document_id] = doc
        return doc


def _participant_payload(participant):
    return {
        'sessionId': participant.get('sessionId'),
        'actorId': participant.get('actorId'),
        'displayName': participant.get('displayName') or '老師',
        'deviceId': participant.get('deviceId') or '',
        'color': participant.get('color') or '#2f80ed',
        'presence': participant.get('presence') or {},
        'lastSeenAt': participant.get('lastSeenAt'),
    }


def _active_participants(doc, exclude_stale=True):
    now_ts = time.time()
    participants = []
    for participant in doc['participants'].values():
        if exclude_stale and now_ts - participant.get('lastSeenTs', now_ts) > STALE_PARTICIPANT_SECONDS:
            continue
        participants.append(_participant_payload(participant))
    return participants


def _next_server_seq(doc):
    doc['serverSeq'] += 1
    return doc['serverSeq']


def _find_block(content_blocks, block_id):
    if not block_id or not isinstance(content_blocks, list):
        return None
    for block in content_blocks:
        if isinstance(block, dict) and block.get('id') == block_id:
            return block
    return None


def _text_value(value):
    return value if isinstance(value, str) else ''


def _clamp_int(value, lower, upper):
    try:
        number = int(value)
    except Exception:
        number = lower
    return max(lower, min(upper, number))


def _transform_text_position(position, history_op, prefer_after_insert=False):
    start = int(history_op.get('start') or 0)
    delete_count = max(0, int(history_op.get('deleteCount') or 0))
    insert_text = history_op.get('insertText') or ''
    insert_len = len(insert_text)
    end = start + delete_count
    delta = insert_len - delete_count

    if position < start:
        return position
    if position == start and delete_count == 0:
        return position + insert_len if prefer_after_insert else position
    if position <= end:
        return start + insert_len
    return position + delta


def _transform_text_operation(operation, history):
    start = max(0, int(operation.get('start') or 0))
    delete_count = max(0, int(operation.get('deleteCount') or 0))
    insert_text = operation.get('insertText') or ''
    end = start + delete_count

    for item in history:
        start = _transform_text_position(start, item, prefer_after_insert=True)
        end = _transform_text_position(end, item, prefer_after_insert=True)
        if end < start:
            end = start

    return {
        'start': start,
        'deleteCount': max(0, end - start),
        'insertText': insert_text,
    }


def _apply_text_operation(text, operation):
    start = _clamp_int(operation.get('start'), 0, len(text))
    delete_count = _clamp_int(operation.get('deleteCount'), 0, len(text) - start)
    insert_text = operation.get('insertText') or ''
    return text[:start] + insert_text + text[start + delete_count:]


def _apply_journal_text_operation(doc, payload, actor):
    snapshot = doc.get('snapshot') or {}
    content_blocks = snapshot.get('contentBlocks') or []
    block_id = payload.get('blockId')
    block = _find_block(content_blocks, block_id)
    if not block:
        return None, {'code': 'block_not_found'}
    if block.get('type') != 'plaintext':
        return None, {'code': 'plaintext_only'}

    current_version = int(doc['blockVersions'].get(block_id, 0))
    base_version = _clamp_int(payload.get('baseVersion'), 0, current_version)
    operation = payload.get('operation') or {}
    if not isinstance(operation, dict):
        return None, {'code': 'operation_required'}

    history = [
        item for item in doc['textHistory'].get(block_id, [])
        if int(item.get('version') or 0) > base_version
    ]
    transformed = _transform_text_operation(operation, history)
    current_text = _text_value(block.get('content'))
    transformed['start'] = _clamp_int(transformed.get('start'), 0, len(current_text))
    transformed['deleteCount'] = _clamp_int(
        transformed.get('deleteCount'),
        0,
        len(current_text) - transformed['start'],
    )

    block['content'] = _apply_text_operation(current_text, transformed)
    edited_by = payload.get('editedBy') or {
        'userId': actor.get('actorId'),
        'cname': actor.get('displayName'),
        'ename': '',
    }
    snapshot['editedBy'] = edited_by
    snapshot['updatedAt'] = _now()
    doc['dirty'] = True

    next_version = current_version + 1
    doc['blockVersions'][block_id] = next_version
    history_item = {
        'version': next_version,
        'sessionId': payload.get('sessionId') or '',
        'start': transformed['start'],
        'deleteCount': transformed['deleteCount'],
        'insertText': transformed.get('insertText') or '',
        'sentAt': _now(),
    }
    block_history = doc['textHistory'].setdefault(block_id, [])
    block_history.append(history_item)
    if len(block_history) > MAX_TEXT_HISTORY_PER_BLOCK:
        del block_history[:-MAX_TEXT_HISTORY_PER_BLOCK]

    return {
        'blockId': block_id,
        'baseVersion': base_version,
        'version': next_version,
        'operation': transformed,
        'contentBlocks': content_blocks,
        'blockVersions': dict(doc['blockVersions']),
        'editedBy': edited_by,
        'cursor': payload.get('cursor'),
    }, None


def _send_json(ws, message):
    try:
        ws.send(json.dumps(message, ensure_ascii=False))
        return True
    except Exception:
        return False


def _broadcast(document_id, message, exclude_session_id=None):
    stale = []
    with _state_lock:
        doc = _documents.get(document_id)
        if not doc:
            return
        clients = list(doc['clients'].items())

    for session_id, ws in clients:
        if exclude_session_id and session_id == exclude_session_id:
            continue
        if not _send_json(ws, message):
            stale.append(session_id)

    if stale:
        with _state_lock:
            doc = _documents.get(document_id)
            if doc:
                for session_id in stale:
                    doc['clients'].pop(session_id, None)
                    doc['participants'].pop(session_id, None)


def _broadcast_participants(document_id):
    with _state_lock:
        doc = _documents.get(document_id)
        if not doc:
            return
        message = {
            'protocolVersion': 1,
            'type': 'presence.sync',
            'documentId': document_id,
            'serverSeq': _next_server_seq(doc),
            'sentAt': _now(),
            'payload': {
                'participants': _active_participants(doc),
            },
        }
    _broadcast(document_id, message)


def _schedule_save(document_id):
    with _state_lock:
        doc = _documents.get(document_id)
        if not doc:
            return
        existing = doc.get('saveTimer')
        if existing:
            existing.cancel()
        timer = threading.Timer(SAVE_DEBOUNCE_SECONDS, _persist_document, args=(document_id,))
        timer.daemon = True
        doc['saveTimer'] = timer
        timer.start()


def _persist_document(document_id):
    with _state_lock:
        doc = _documents.get(document_id)
        if not doc or not doc.get('dirty'):
            return
        snapshot = json.loads(json.dumps(doc['snapshot'], ensure_ascii=False))
        doc['dirty'] = False
        doc['saveTimer'] = None

    parsed = _parse_document_id(document_id)
    if not parsed:
        return

    if parsed['kind'] == 'note':
        result = _save_note_snapshot(parsed, snapshot)
        with _state_lock:
            doc = _documents.get(document_id)
            if not doc:
                return
            doc['snapshot']['updatedAt'] = result.get('updatedAt')
            message = {
                'protocolVersion': 1,
                'type': 'snapshot.saved',
                'documentId': document_id,
                'serverSeq': _next_server_seq(doc),
                'sentAt': _now(),
                'payload': {
                    'updatedAt': result.get('updatedAt'),
                },
            }
        _broadcast(document_id, message)
        _send_data_updated(parsed, result)
        return

    if parsed['kind'] != 'journal':
        return

    edited_by = snapshot.get('editedBy') or {}
    result = data_service.save_class_journal(
        parsed['className'],
        parsed['date'],
        snapshot.get('contentBlocks', []),
        edited_by,
    )

    with _state_lock:
        doc = _documents.get(document_id)
        if not doc:
            return
        doc['snapshot']['updatedAt'] = result.get('updatedAt')
        message = {
            'protocolVersion': 1,
            'type': 'snapshot.saved',
            'documentId': document_id,
            'serverSeq': _next_server_seq(doc),
            'sentAt': _now(),
            'payload': {
                'updatedAt': result.get('updatedAt'),
            },
        }

    _broadcast(document_id, message)
    _send_data_updated(parsed, result)


def _save_note_snapshot(parsed, snapshot):
    note_data = dict(snapshot.get('note') or {})
    edited_by_raw = snapshot.get('editedBy') or {}
    year, month, _day = map(int, parsed['date'].split('-'))
    now = datetime.now().isoformat()

    raw_items = note_data.pop('itemsToBring', None)
    if raw_items and isinstance(raw_items, list) and len(raw_items) > 0:
        items_to_bring = json.dumps({'items': raw_items}, ensure_ascii=False)
    else:
        items_to_bring = None

    raw_returned = note_data.pop('returnedItems', None)
    if raw_returned and isinstance(raw_returned, list) and len(raw_returned) > 0:
        returned_items = json.dumps(raw_returned, ensure_ascii=False)
    else:
        returned_items = None

    survey_id = note_data.pop('surveyId', None) or None
    edited_by = json.dumps({
        'userId': edited_by_raw.get('userId', ''),
        'cname': edited_by_raw.get('cname', ''),
        'ename': edited_by_raw.get('ename', ''),
        'editedAt': now,
    }, ensure_ascii=False) if edited_by_raw else None

    conn = data_service.get_db()
    try:
        row = conn.execute(
            'SELECT id, status FROM contact_books WHERE student_id = ? AND date = ?',
            (parsed['studentId'], parsed['date'])
        ).fetchone()
        teacher_json = json.dumps(note_data, ensure_ascii=False)
        if not row:
            conn.execute('''
                INSERT INTO contact_books (student_id, date, year, month, status, original_teacher,
                    items_to_bring, returned_items, survey_id, edited_by, last_modified)
                VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)
            ''', (parsed['studentId'], parsed['date'], year, month, teacher_json,
                  items_to_bring, returned_items, survey_id, edited_by, now))
        else:
            current_status = row['status']
            new_status = current_status if current_status in ('notified', 'read', 'signed') else 'draft'
            conn.execute('''
                UPDATE contact_books SET original_teacher = ?, items_to_bring = ?,
                    returned_items = ?, survey_id = ?, edited_by = ?, status = ?, last_modified = ?
                WHERE student_id = ? AND date = ?
            ''', (teacher_json, items_to_bring, returned_items, survey_id, edited_by,
                  new_status, now, parsed['studentId'], parsed['date']))
        conn.commit()
        return {'updatedAt': now, 'studentId': parsed['studentId'], 'date': parsed['date']}
    finally:
        conn.close()


def _send_data_updated(parsed, result):
    def _notify():
        try:
            from services.send_notification import send_to_role
            if parsed['kind'] == 'note':
                notify_data = {
                    'type': 'data_updated',
                    'dataType': 'student_notes',
                    'date': parsed['date'],
                    'studentIds': json.dumps([parsed['studentId']], ensure_ascii=False),
                    'updatedAt': result.get('updatedAt', ''),
                }
            else:
                notify_data = {
                    'type': 'data_updated',
                    'dataType': 'class_journal',
                    'className': parsed['className'],
                    'date': parsed['date'],
                    'updatedAt': result.get('updatedAt', ''),
                }
            send_to_role(data_service, 'teacher', '', '', notify_data)
            send_to_role(data_service, 'admin', '', '', notify_data)
        except Exception as e:
            print(f'[Collab] data_updated notification error: {e}')

    threading.Thread(target=_notify, daemon=True).start()


@collab_bp.route('/documents/<path:document_id>/bootstrap', methods=['GET'])
def bootstrap_document(document_id):
    doc = _ensure_document(document_id)
    if not doc:
        return jsonify({'error': 'unsupported or invalid documentId'}), 400

    with _state_lock:
        return jsonify({
            'documentId': document_id,
            'serverSeq': doc['serverSeq'],
            'snapshot': doc['snapshot'],
            'blockVersions': dict(doc.get('blockVersions') or {}),
            'participants': _active_participants(doc),
        })


@collab_sock.route('/api/collab/ws')
def collab_ws(ws):
    document_id = request.args.get('documentId', '')
    doc = _ensure_document(document_id)
    if not doc:
        _send_json(ws, {'type': 'error', 'payload': {'code': 'invalid_document'}})
        return

    session_id = request.args.get('sessionId') or f"sess_{uuid.uuid4().hex[:12]}"
    actor = {
        'actorId': request.args.get('actorId', ''),
        'displayName': request.args.get('displayName') or request.args.get('actorId') or '老師',
        'deviceId': request.args.get('deviceId', ''),
        'color': request.args.get('color') or '#2f80ed',
    }
    participant = {
        **actor,
        'sessionId': session_id,
        'presence': {},
        'connectedAt': _now(),
        'lastSeenAt': _now(),
        'lastSeenTs': time.time(),
    }

    with _state_lock:
        doc['clients'][session_id] = ws
        doc['participants'][session_id] = participant
        hello = {
            'protocolVersion': 1,
            'type': 'snapshot',
            'documentId': document_id,
            'sessionId': session_id,
            'serverSeq': _next_server_seq(doc),
            'sentAt': _now(),
            'actor': actor,
            'payload': {
                'snapshot': doc['snapshot'],
                'blockVersions': dict(doc.get('blockVersions') or {}),
                'participants': _active_participants(doc),
            },
        }

    _send_json(ws, hello)
    _broadcast_participants(document_id)

    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                message = json.loads(raw)
            except Exception:
                _send_json(ws, {'type': 'error', 'payload': {'code': 'bad_json'}})
                continue

            msg_type = message.get('type')
            payload = message.get('payload') or {}

            with _state_lock:
                doc = _documents.get(document_id)
                if not doc:
                    break
                participant = doc['participants'].get(session_id)
                if participant:
                    participant['lastSeenAt'] = _now()
                    participant['lastSeenTs'] = time.time()

            if msg_type == 'ping':
                _send_json(ws, {'type': 'pong', 'documentId': document_id, 'sentAt': _now()})
                continue

            if msg_type == 'presence.update':
                with _state_lock:
                    doc = _documents.get(document_id)
                    participant = doc['participants'].get(session_id) if doc else None
                    if not doc or not participant:
                        continue
                    participant['presence'] = payload.get('presence') or payload
                    event = {
                        'protocolVersion': 1,
                        'type': 'presence.update',
                        'documentId': document_id,
                        'sessionId': session_id,
                        'serverSeq': _next_server_seq(doc),
                        'sentAt': _now(),
                        'actor': actor,
                        'payload': {
                            'participant': _participant_payload(participant),
                        },
                    }
                _broadcast(document_id, event, exclude_session_id=session_id)
                continue

            if msg_type == 'text.operation':
                parsed = _parse_document_id(document_id)
                if not parsed or parsed['kind'] != 'journal':
                    _send_json(ws, {'type': 'error', 'payload': {'code': 'journal_required'}})
                    continue

                payload['sessionId'] = session_id
                with _state_lock:
                    doc = _documents.get(document_id)
                    if not doc:
                        continue
                    result_payload, error_payload = _apply_journal_text_operation(doc, payload, actor)
                    if error_payload:
                        _send_json(ws, {'type': 'error', 'payload': error_payload})
                        continue
                    participant = doc['participants'].get(session_id)
                    if participant:
                        participant['presence'] = {
                            'focus': {
                                'path': document_id,
                                'field': 'contentBlocks',
                                'blockId': result_payload.get('blockId'),
                            },
                            'isTyping': True,
                            'cursor': payload.get('cursor'),
                        }
                    event = {
                        'protocolVersion': 1,
                        'type': 'text.operation',
                        'documentId': document_id,
                        'sessionId': session_id,
                        'clientSeq': message.get('clientSeq'),
                        'serverSeq': _next_server_seq(doc),
                        'sentAt': _now(),
                        'actor': actor,
                        'payload': result_payload,
                    }
                    presence_event = {
                        'protocolVersion': 1,
                        'type': 'presence.update',
                        'documentId': document_id,
                        'sessionId': session_id,
                        'serverSeq': _next_server_seq(doc),
                        'sentAt': _now(),
                        'actor': actor,
                        'payload': {
                            'participant': _participant_payload(participant),
                        },
                    } if participant else None
                _broadcast(document_id, event)
                if presence_event:
                    _broadcast(document_id, presence_event, exclude_session_id=session_id)
                _schedule_save(document_id)
                continue

            if msg_type == 'doc.update':
                parsed = _parse_document_id(document_id)
                content_blocks = payload.get('contentBlocks')
                note_payload = payload.get('note')
                if parsed and parsed['kind'] == 'journal' and not isinstance(content_blocks, list):
                    _send_json(ws, {'type': 'error', 'payload': {'code': 'contentBlocks_required'}})
                    continue
                if parsed and parsed['kind'] == 'note' and not isinstance(note_payload, dict):
                    _send_json(ws, {'type': 'error', 'payload': {'code': 'note_required'}})
                    continue
                edited_by = payload.get('editedBy') or {
                    'userId': actor.get('actorId'),
                    'cname': actor.get('displayName'),
                    'ename': '',
                }
                with _state_lock:
                    doc = _documents.get(document_id)
                    if not doc:
                        continue
                    if parsed and parsed['kind'] == 'note':
                        doc['snapshot']['note'] = note_payload
                    else:
                        doc['snapshot']['contentBlocks'] = content_blocks
                    doc['snapshot']['editedBy'] = edited_by
                    doc['snapshot']['updatedAt'] = _now()
                    doc['dirty'] = True
                    next_payload = {'editedBy': edited_by}
                    if parsed and parsed['kind'] == 'note':
                        next_payload['note'] = note_payload
                    else:
                        next_payload['contentBlocks'] = content_blocks
                        doc['blockVersions'] = {}
                        doc['textHistory'] = {}
                        next_payload['blockVersions'] = dict(doc['blockVersions'])
                    event = {
                        'protocolVersion': 1,
                        'type': 'doc.update',
                        'documentId': document_id,
                        'sessionId': session_id,
                        'clientSeq': message.get('clientSeq'),
                        'serverSeq': _next_server_seq(doc),
                        'sentAt': _now(),
                        'actor': actor,
                        'payload': next_payload,
                    }
                _broadcast(document_id, event, exclude_session_id=session_id)
                _schedule_save(document_id)
                continue

            _send_json(ws, {'type': 'error', 'payload': {'code': 'unknown_type', 'type': msg_type}})
    finally:
        with _state_lock:
            doc = _documents.get(document_id)
            if doc:
                doc['clients'].pop(session_id, None)
                doc['participants'].pop(session_id, None)
        _broadcast_participants(document_id)

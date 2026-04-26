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
    return None


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
    if not parsed or parsed['kind'] != 'journal':
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


def _send_data_updated(parsed, result):
    def _notify():
        try:
            from services.send_notification import send_to_role
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

            if msg_type == 'doc.update':
                content_blocks = payload.get('contentBlocks')
                if not isinstance(content_blocks, list):
                    _send_json(ws, {'type': 'error', 'payload': {'code': 'contentBlocks_required'}})
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
                    doc['snapshot']['contentBlocks'] = content_blocks
                    doc['snapshot']['editedBy'] = edited_by
                    doc['snapshot']['updatedAt'] = _now()
                    doc['dirty'] = True
                    event = {
                        'protocolVersion': 1,
                        'type': 'doc.update',
                        'documentId': document_id,
                        'sessionId': session_id,
                        'clientSeq': message.get('clientSeq'),
                        'serverSeq': _next_server_seq(doc),
                        'sentAt': _now(),
                        'actor': actor,
                        'payload': {
                            'contentBlocks': content_blocks,
                            'editedBy': edited_by,
                        },
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

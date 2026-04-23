from flask import Blueprint, request, jsonify
from datetime import datetime
from services.data_service import DataService
import os
import json
import threading
import random

news_bp = Blueprint('news', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

def _migrate():
    """Auto-add columns added after initial schema."""
    conn = data_service.get_db()
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(news)').fetchall()]
        if 'ref_no' not in cols:
            conn.execute('ALTER TABLE news ADD COLUMN ref_no VARCHAR(50)')
        if 'first_published_at' not in cols:
            conn.execute('ALTER TABLE news ADD COLUMN first_published_at VARCHAR(50)')
        if 'pending_draft' not in cols:
            conn.execute('ALTER TABLE news ADD COLUMN pending_draft TEXT')
        if 'pin_order' not in cols:
            conn.execute('ALTER TABLE news ADD COLUMN pin_order INTEGER')
        conn.commit()
    finally:
        conn.close()

_migrate()

def load_json(val):
    if not val: return None
    try: return json.loads(val)
    except: return None

def _gen_block_id():
    import time
    ts = int(time.time() * 1000)
    rand = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=5))
    return f'blk_{ts}_{rand}'

def _normalize_blocks(blocks):
    """Ensure every block has an id. Migrates legacy blocks on-the-fly."""
    if not blocks:
        return blocks
    result = []
    for b in blocks:
        if 'id' not in b:
            b = dict(b)
            b['id'] = _gen_block_id()
        result.append(b)
    return result

def format_news(r):
    keys = r.keys()
    pending_draft_raw = r['pending_draft'] if 'pending_draft' in keys else None
    pending_draft = load_json(pending_draft_raw) if pending_draft_raw else None
    return {
        'id': r['id'],
        'title': r['title'],
        'tag': r['tag'],
        'coverImage': r['cover_image'],
        'contentBlocks': _normalize_blocks(load_json(r['content_blocks']) or []),
        'author': r['author'],
        'isPinned': bool(r['is_pinned']),
        'publishAt': r['publish_at'],
        'createdAt': r['created_at'],
        'updatedAt': r['updated_at'],
        'createdBy': r['created_by'],
        'surveyId': r['survey_id'],
        'targetClasses': load_json(r['target_classes']) or [],
        'status': r['status'],
        'refNo': r['ref_no'] if 'ref_no' in keys else None,
        'firstPublishedAt': r['first_published_at'] if 'first_published_at' in keys else None,
        'pendingDraft': pending_draft,
        'hasDraft': pending_draft is not None,
        'pinOrder': r['pin_order'] if 'pin_order' in keys else None,
    }

@news_bp.route('/', methods=['GET'])
def list_news():
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 10, type=int)
    target_class = request.args.get('class', None)
    status_filter = request.args.get('status', 'published')

    conn = data_service.get_db()
    try:
        now = datetime.now().isoformat()

        # Build query — 'all' means no status filter (admin/teacher view)
        if status_filter == 'all':
            query = "SELECT * FROM news"
            params = []
        else:
            query = "SELECT * FROM news WHERE status = ?"
            params = [status_filter]

        rows = conn.execute(query, params).fetchall()
        filtered = []
        for r in rows:
            # Check publishAt
            pub = r['publish_at']
            if pub and pub > now:
                continue

            # Check target_classes
            t_classes = load_json(r['target_classes']) or []
            if target_class and t_classes and target_class not in t_classes:
                continue

            filtered.append(format_news(r))

        # Sort: pinned first by pin_order ASC (None last), then non-pinned by publishAt DESC
        pinned = [x for x in filtered if x.get('isPinned')]
        non_pinned = [x for x in filtered if not x.get('isPinned')]
        pinned.sort(key=lambda x: (x.get('pinOrder') is None, x.get('pinOrder') or 0))
        non_pinned.sort(key=lambda x: x.get('publishAt', ''), reverse=True)
        filtered = pinned + non_pinned

        # Pagination
        total = len(filtered)
        start = (page - 1) * limit
        end = start + limit
        paginated = filtered[start:end]

        result = []
        for item in paginated:
            preview = ''
            first_image = None
            for block in item.get('contentBlocks', []):
                if block.get('type') == 'text' and not preview:
                    preview = block.get('content', '')[:100]
                elif block.get('type') == 'image' and not first_image:
                    first_image = block.get('url')

            display_image = item.get('coverImage') or first_image
            result.append({
                'id': item.get('id'),
                'title': item.get('title'),
                'tag': item.get('tag'),
                'coverImage': display_image,
                'preview': preview,
                'author': item.get('author'),
                'isPinned': item.get('isPinned', False),
                'publishAt': item.get('publishAt'),
                'surveyId': item.get('surveyId'),
                'status': item.get('status'),
                'refNo': item.get('refNo'),
                'firstPublishedAt': item.get('firstPublishedAt'),
                'updatedAt': item.get('updatedAt'),
                'hasDraft': item.get('hasDraft', False),
                'pinOrder': item.get('pinOrder'),
            })

        return jsonify({
            'items': result,
            'total': total,
            'page': page,
            'limit': limit,
            'hasMore': end < total
        })
    finally:
        conn.close()

@news_bp.route('/<news_id>', methods=['GET'])
def get_news_detail(news_id):
    conn = data_service.get_db()
    try:
        r = conn.execute('SELECT * FROM news WHERE id = ?', (news_id,)).fetchone()
        if not r:
            return jsonify({'error': 'News not found'}), 404
        return jsonify(format_news(r))
    finally:
        conn.close()

@news_bp.route('/', methods=['POST'])
def create_news():
    data = request.json
    if not data or 'title' not in data:
        return jsonify({'error': 'Title required'}), 400

    now = datetime.now()
    base_id = f"news-{now.strftime('%Y%m%d')}"

    conn = data_service.get_db()
    try:
        row = conn.execute("SELECT count(*) as cnt FROM news WHERE id LIKE ?", (f"{base_id}%",)).fetchone()
        seq = row['cnt'] + 1
        news_id = f"{base_id}-{seq:03d}"

        status = data.get('status', 'published')
        first_published_at = now.isoformat() if status == 'published' else None

        conn.execute('''
            INSERT INTO news (
                id, title, tag, cover_image, content_blocks, author,
                is_pinned, publish_at, created_at, updated_at, created_by,
                survey_id, target_classes, status, ref_no, first_published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            news_id, data.get('title'), data.get('tag', '一般公告'), data.get('coverImage'),
            json.dumps(data.get('contentBlocks', []), ensure_ascii=False), data.get('author'),
            1 if data.get('isPinned') else 0, data.get('publishAt', now.isoformat()),
            now.isoformat(), now.isoformat(), data.get('createdBy', 'admin'),
            data.get('surveyId'), json.dumps(data.get('targetClasses', []), ensure_ascii=False),
            status, data.get('refNo'), first_published_at
        ))
        conn.commit()

        r = conn.execute('SELECT * FROM news WHERE id = ?', (news_id,)).fetchone()
        return jsonify(format_news(r)), 201
    finally:
        conn.close()

@news_bp.route('/<news_id>', methods=['PUT'])
def update_news(news_id):
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    conn = data_service.get_db()
    try:
        r = conn.execute('SELECT * FROM news WHERE id = ?', (news_id,)).fetchone()
        if not r:
            return jsonify({'error': 'News not found'}), 404

        keys = r.keys()
        old_status = r['status']
        new_status = data.get('status', old_status)
        now = datetime.now()

        # Published article being saved as draft:
        # Store edits in pending_draft only — the published version remains visible to users.
        if old_status == 'published' and new_status == 'draft':
            draft_payload = {
                'title': data.get('title', r['title']),
                'tag': data.get('tag', r['tag']),
                'coverImage': data.get('coverImage', r['cover_image']),
                'contentBlocks': data.get('contentBlocks', load_json(r['content_blocks']) or []),
                'author': data.get('author', r['author']),
                'isPinned': data.get('isPinned', bool(r['is_pinned'])),
                'publishAt': data.get('publishAt', r['publish_at']),
                'surveyId': data.get('surveyId', r['survey_id']),
                'targetClasses': data.get('targetClasses', load_json(r['target_classes']) or []),
                'refNo': data.get('refNo', r['ref_no'] if 'ref_no' in keys else None),
            }
            conn.execute(
                'UPDATE news SET pending_draft=?, updated_at=? WHERE id=?',
                (json.dumps(draft_payload, ensure_ascii=False), now.isoformat(), news_id)
            )
            conn.commit()
            r2 = conn.execute('SELECT * FROM news WHERE id = ?', (news_id,)).fetchone()
            return jsonify(format_news(r2))

        # All other cases: apply changes to main columns.
        news = format_news(r)

        old_pinned = bool(r['is_pinned'])
        updatable = ['title', 'tag', 'coverImage', 'contentBlocks', 'author',
                     'isPinned', 'publishAt', 'surveyId', 'targetClasses', 'status', 'refNo']
        for field in updatable:
            if field in data:
                news[field] = data[field]

        new_pinned = bool(news['isPinned'])

        # Determine pin_order value
        current_pin_order = r['pin_order'] if 'pin_order' in keys else None
        if 'pinOrder' in data:
            # Explicit reorder from teacher web (↑↓ buttons) — just use the provided value
            pin_order_val = data['pinOrder']
        elif new_pinned and not old_pinned:
            # Newly pinned: bump all other pinned articles, place this one at top (0)
            conn.execute(
                'UPDATE news SET pin_order = pin_order + 1 WHERE is_pinned = 1 AND id != ?',
                (news_id,)
            )
            pin_order_val = 0
        elif not new_pinned:
            # Unpinned: clear pin_order
            pin_order_val = None
        else:
            # No change to pinned state — keep existing pin_order
            pin_order_val = current_pin_order

        # Set first_published_at on first publish
        current_first_pub = r['first_published_at'] if 'first_published_at' in keys else None
        new_first_pub = current_first_pub
        if news['status'] == 'published' and not current_first_pub:
            new_first_pub = now.isoformat()

        # Clear pending_draft when publishing; preserve for other transitions (e.g. published→hidden)
        pending_draft_val = None if news['status'] == 'published' else (
            r['pending_draft'] if 'pending_draft' in keys else None
        )

        conn.execute('''
            UPDATE news SET title=?, tag=?, cover_image=?, content_blocks=?, author=?,
            is_pinned=?, publish_at=?, survey_id=?, target_classes=?, status=?, updated_at=?, ref_no=?,
            first_published_at=?, pending_draft=?, pin_order=?
            WHERE id=?
        ''', (
            news['title'], news['tag'], news['coverImage'], json.dumps(news['contentBlocks'], ensure_ascii=False),
            news['author'], 1 if news['isPinned'] else 0, news['publishAt'], news['surveyId'],
            json.dumps(news['targetClasses'], ensure_ascii=False), news['status'], now.isoformat(),
            news.get('refNo'), new_first_pub, pending_draft_val, pin_order_val, news_id
        ))
        conn.commit()

        r2 = conn.execute('SELECT * FROM news WHERE id = ?', (news_id,)).fetchone()
        return jsonify(format_news(r2))
    finally:
        conn.close()

@news_bp.route('/<news_id>', methods=['DELETE'])
def delete_news(news_id):
    conn = data_service.get_db()
    try:
        cursor = conn.execute('DELETE FROM news WHERE id = ?', (news_id,))
        if cursor.rowcount == 0:
            return jsonify({'error': 'News not found'}), 404
        conn.commit()
        return jsonify({'status': 'deleted'}), 200
    finally:
        conn.close()


@news_bp.route('/<news_id>/notify', methods=['POST'])
def notify_news(news_id):
    """Send push notification for a news item.
    Optional JSON body: { "body": "custom notification body text" }
    """
    conn = data_service.get_db()
    try:
        r = conn.execute('SELECT * FROM news WHERE id = ?', (news_id,)).fetchone()
        if not r:
            return jsonify({'error': 'News not found'}), 404
        body_text = ''
        if request.is_json and request.json:
            body_text = request.json.get('body', '')
        _notify_announcement_bg(news_id, r['title'], body_text)
        return jsonify({'status': 'queued'})
    finally:
        conn.close()


@news_bp.route('/<news_id>/notify-silent', methods=['POST'])
def notify_news_silent(news_id):
    """Send silent data-only push for announcement cache refresh (no user-visible banner)."""
    conn = data_service.get_db()
    try:
        r = conn.execute('SELECT id FROM news WHERE id = ?', (news_id,)).fetchone()
        if not r:
            return jsonify({'error': 'News not found'}), 404
        _notify_announcement_update_bg(news_id)
        return jsonify({'status': 'queued'})
    finally:
        conn.close()


def _notify_announcement_bg(news_id, title_text, body_text=''):
    """Send announcement notification to all parents in a background thread."""
    def _send():
        try:
            from services.send_notification import notify_parents_announcement
            notify_parents_announcement(data_service, news_id, title_text, body_text)
        except Exception as e:
            print(f'[Notification] Error sending announcement notification: {e}')
    threading.Thread(target=_send, daemon=True).start()


def _notify_announcement_update_bg(news_id):
    """Send silent announcement-update push to refresh app data without visible notification."""
    def _send():
        try:
            from services.send_notification import notify_parents_announcement_update
            notify_parents_announcement_update(data_service, news_id)
        except Exception as e:
            print(f'[Notification] Error sending silent announcement update: {e}')
    threading.Thread(target=_send, daemon=True).start()

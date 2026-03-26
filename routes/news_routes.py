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
        'status': r['status']
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

        # Execute query first, then filter targetClasses manually (simplest way since it's JSON array in sqlite)
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
            
        # Sort
        filtered.sort(key=lambda x: (
            not x.get('isPinned', False),
            x.get('publishAt', '') if x.get('publishAt') else ''
        ), reverse=False)
        
        pinned = [x for x in filtered if x.get('isPinned')]
        non_pinned = [x for x in filtered if not x.get('isPinned')]
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
                'surveyId': item.get('surveyId')
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
        
        conn.execute('''
            INSERT INTO news (
                id, title, tag, cover_image, content_blocks, author,
                is_pinned, publish_at, created_at, updated_at, created_by,
                survey_id, target_classes, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            news_id, data.get('title'), data.get('tag', '一般公告'), data.get('coverImage'),
            json.dumps(data.get('contentBlocks', []), ensure_ascii=False), data.get('author'),
            1 if data.get('isPinned') else 0, data.get('publishAt', now.isoformat()),
            now.isoformat(), now.isoformat(), data.get('createdBy', 'admin'),
            data.get('surveyId'), json.dumps(data.get('targetClasses', []), ensure_ascii=False),
            data.get('status', 'published')
        ))
        conn.commit()
        
        # Return new item
        r = conn.execute('SELECT * FROM news WHERE id = ?', (news_id,)).fetchone()
        result = format_news(r)

        # Notify parents if published immediately (no future publishAt)
        if data.get('status', 'published') == 'published':
            pub_at = data.get('publishAt')
            is_future = pub_at and pub_at > now.isoformat() if pub_at else False
            if not is_future:
                _notify_announcement_bg(news_id, data.get('title', ''))

        return jsonify(result), 201
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
            
        news = format_news(r)
        
        updatable = ['title', 'tag', 'coverImage', 'contentBlocks', 'author', 
                     'isPinned', 'publishAt', 'surveyId', 'targetClasses', 'status']
        for field in updatable:
            if field in data:
                news[field] = data[field]
        
        old_status = r['status']

        conn.execute('''
            UPDATE news SET title=?, tag=?, cover_image=?, content_blocks=?, author=?,
            is_pinned=?, publish_at=?, survey_id=?, target_classes=?, status=?, updated_at=?
            WHERE id=?
        ''', (
            news['title'], news['tag'], news['coverImage'], json.dumps(news['contentBlocks'], ensure_ascii=False),
            news['author'], 1 if news['isPinned'] else 0, news['publishAt'], news['surveyId'],
            json.dumps(news['targetClasses'], ensure_ascii=False), news['status'], datetime.now().isoformat(),
            news_id
        ))
        conn.commit()

        # Notify parents if status just changed to 'published'
        if old_status != 'published' and news['status'] == 'published':
            _notify_announcement_bg(news_id, news['title'])

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


def _notify_announcement_bg(news_id, title_text):
    """Send announcement notification to all parents in a background thread."""
    def _send():
        try:
            from services.send_notification import notify_parents_announcement
            notify_parents_announcement(data_service, news_id, title_text, '')
        except Exception as e:
            print(f'[Notification] Error sending announcement notification: {e}')
    threading.Thread(target=_send, daemon=True).start()

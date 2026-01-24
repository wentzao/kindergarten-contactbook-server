from flask import Blueprint, request, jsonify
from datetime import datetime
import os
import json

news_bp = Blueprint('news', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
NEWS_DIR = os.path.join(DATA_DIR, 'news')

def get_all_news():
    """Load all news from definitions folder"""
    news_list = []
    if not os.path.exists(NEWS_DIR):
        return news_list
    
    for filename in os.listdir(NEWS_DIR):
        if filename.endswith('.json'):
            filepath = os.path.join(NEWS_DIR, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    news_item = json.load(f)
                    news_list.append(news_item)
            except Exception as e:
                print(f"Error loading {filename}: {e}")
    
    return news_list

def get_news_by_id(news_id):
    """Get a single news item by ID"""
    filepath = os.path.join(NEWS_DIR, f"{news_id}.json")
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

@news_bp.route('/', methods=['GET'])
def list_news():
    """
    Get news list with pagination and filtering.
    Query params:
      - page: Page number (default 1)
      - limit: Items per page (default 10)
      - class: Filter by target class
      - status: Filter by status (default 'published')
    """
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 10, type=int)
    target_class = request.args.get('class', None)
    status_filter = request.args.get('status', 'published')
    
    all_news = get_all_news()
    now = datetime.now()
    
    # Filter by status and publishAt
    filtered = []
    for item in all_news:
        # Check status
        if item.get('status') != status_filter:
            continue
        
        # Check publishAt (only show published items)
        publish_at = item.get('publishAt')
        if publish_at:
            try:
                pub_time = datetime.fromisoformat(publish_at.replace('Z', '+00:00'))
                if pub_time > now:
                    continue
            except:
                pass
        
        # Check target class
        target_classes = item.get('targetClasses', [])
        if target_class and target_classes and target_class not in target_classes:
            continue
        
        filtered.append(item)
    
    # Sort: pinned first, then by publishAt descending
    filtered.sort(key=lambda x: (
        not x.get('isPinned', False),  # Pinned first
        x.get('publishAt', '') if x.get('publishAt') else ''
    ), reverse=False)
    
    # Re-sort non-pinned by date descending
    pinned = [x for x in filtered if x.get('isPinned')]
    non_pinned = [x for x in filtered if not x.get('isPinned')]
    non_pinned.sort(key=lambda x: x.get('publishAt', ''), reverse=True)
    filtered = pinned + non_pinned
    
    # Pagination
    total = len(filtered)
    start = (page - 1) * limit
    end = start + limit
    paginated = filtered[start:end]
    
    # Prepare response (summary for list view)
    result = []
    for item in paginated:
        # Extract preview text from contentBlocks
        preview = ''
        first_image = None
        for block in item.get('contentBlocks', []):
            if block.get('type') == 'text' and not preview:
                preview = block.get('content', '')[:100]
            elif block.get('type') == 'image' and not first_image:
                first_image = block.get('url')
        
        # Use coverImage if set, otherwise fallback to first image in contentBlocks
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

@news_bp.route('/<news_id>', methods=['GET'])
def get_news_detail(news_id):
    """Get full news item details"""
    news = get_news_by_id(news_id)
    if not news:
        return jsonify({'error': 'News not found'}), 404
    return jsonify(news)

@news_bp.route('/', methods=['POST'])
def create_news():
    """Create a new news item (for admin/teacher)"""
    data = request.json
    if not data or 'title' not in data:
        return jsonify({'error': 'Title required'}), 400
    
    # Generate ID
    now = datetime.now()
    base_id = f"news-{now.strftime('%Y%m%d')}"
    
    # Find next sequence number
    existing = [f for f in os.listdir(NEWS_DIR) if f.startswith(base_id)]
    seq = len(existing) + 1
    news_id = f"{base_id}-{seq:03d}"
    
    news_item = {
        'id': news_id,
        'title': data.get('title'),
        'tag': data.get('tag', '一般公告'),
        'coverImage': data.get('coverImage'),
        'contentBlocks': data.get('contentBlocks', []),
        'author': data.get('author'),
        'isPinned': data.get('isPinned', False),
        'publishAt': data.get('publishAt', now.isoformat()),
        'createdAt': now.isoformat(),
        'updatedAt': now.isoformat(),
        'createdBy': data.get('createdBy', 'admin'),
        'surveyId': data.get('surveyId'),
        'targetClasses': data.get('targetClasses', []),
        'status': data.get('status', 'published')
    }
    
    filepath = os.path.join(NEWS_DIR, f"{news_id}.json")
    os.makedirs(NEWS_DIR, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(news_item, f, ensure_ascii=False, indent=2)
    
    return jsonify(news_item), 201

@news_bp.route('/<news_id>', methods=['PUT'])
def update_news(news_id):
    """Update an existing news item"""
    news = get_news_by_id(news_id)
    if not news:
        return jsonify({'error': 'News not found'}), 404
    
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    # Update fields
    updatable = ['title', 'tag', 'coverImage', 'contentBlocks', 'author', 
                 'isPinned', 'publishAt', 'surveyId', 'targetClasses', 'status']
    for field in updatable:
        if field in data:
            news[field] = data[field]
    
    news['updatedAt'] = datetime.now().isoformat()
    
    filepath = os.path.join(NEWS_DIR, f"{news_id}.json")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(news, f, ensure_ascii=False, indent=2)
    
    return jsonify(news)

@news_bp.route('/<news_id>', methods=['DELETE'])
def delete_news(news_id):
    """Delete a news item"""
    filepath = os.path.join(NEWS_DIR, f"{news_id}.json")
    if not os.path.exists(filepath):
        return jsonify({'error': 'News not found'}), 404
    
    os.remove(filepath)
    return jsonify({'status': 'deleted'}), 200

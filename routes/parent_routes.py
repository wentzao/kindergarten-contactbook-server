from flask import Blueprint, Response, jsonify, request
from services.data_service import DataService
from datetime import datetime
import os

parent_bp = Blueprint('parent', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

# Relation rules:
# father / mother       — at most 1 per student each
# paternal_grandfather / paternal_grandmother
# maternal_grandfather  / maternal_grandmother — at most 2 per student for each
#   (covering both 阿公/阿嬤 and 外公/外婆 slots)
RELATION_LABELS = {
    'father':                '爸爸',
    'mother':                '媽媽',
    'paternal_grandfather':  '阿公',
    'paternal_grandmother':  '阿嬤',
    'maternal_grandfather':  '外公',
    'maternal_grandmother':  '外婆',
}
# Maximum number of users allowed per relation per student
RELATION_LIMITS = {
    'father':                1,
    'mother':                1,
    'paternal_grandfather':  1,
    'paternal_grandmother':  1,
    'maternal_grandfather':  1,
    'maternal_grandmother':  1,
}


def _ensure_relations_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS parent_student_relations (
            user_id    VARCHAR(100) NOT NULL,
            student_id VARCHAR(50)  NOT NULL,
            relation   VARCHAR(50)  NOT NULL,
            updated_at VARCHAR(50),
            PRIMARY KEY (user_id, student_id)
        )
    ''')
    conn.commit()


@parent_bp.route('/<user_id>/avatar', methods=['GET'])
def get_avatar(user_id):
    """Serve cached parent LINE avatar blob."""
    conn = data_service.get_db()
    try:
        row = conn.execute(
            'SELECT picture_data, picture_mime FROM parent_profiles WHERE user_id = ?',
            (user_id,)
        ).fetchone()
        if row and row['picture_data']:
            mime = row['picture_mime'] or 'image/jpeg'
            return Response(row['picture_data'], mimetype=mime,
                            headers={'Cache-Control': 'public, max-age=3600'})
        return Response(status=404)
    finally:
        conn.close()


@parent_bp.route('/<user_id>/relation', methods=['POST'])
def set_relation(user_id):
    """Set or update a parent's relation to a student.

    Body JSON: { "student_id": "...", "relation": "father" }
    """
    data = request.get_json(silent=True) or {}
    student_id = data.get('student_id', '').strip()
    relation   = data.get('relation', '').strip()

    if not student_id or not relation:
        return jsonify({'error': 'student_id and relation are required'}), 400
    if relation not in RELATION_LABELS:
        return jsonify({'error': f'invalid relation; allowed: {list(RELATION_LABELS.keys())}'}), 400

    conn = data_service.get_db()
    try:
        _ensure_relations_table(conn)

        # Check uniqueness limit
        limit = RELATION_LIMITS[relation]
        existing = conn.execute(
            '''SELECT user_id FROM parent_student_relations
               WHERE student_id = ? AND relation = ? AND user_id != ?''',
            (student_id, relation, user_id)
        ).fetchall()
        if len(existing) >= limit:
            label = RELATION_LABELS[relation]
            return jsonify({'error': f'此學生已有 {limit} 位{label}，無法再新增'}), 409

        now_iso = datetime.now().isoformat()
        conn.execute('''
            INSERT INTO parent_student_relations (user_id, student_id, relation, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, student_id) DO UPDATE SET relation = excluded.relation, updated_at = excluded.updated_at
        ''', (user_id, student_id, relation, now_iso))
        conn.commit()
        return jsonify({'ok': True, 'relation': relation, 'label': RELATION_LABELS[relation]}), 200
    finally:
        conn.close()


@parent_bp.route('/student/<student_id>/guardians', methods=['GET'])
def get_student_guardians(student_id):
    """Return all parents bound to a student with profile + relation info."""
    conn = data_service.get_db()
    try:
        _ensure_relations_table(conn)

        rows = conn.execute(
            '''SELECT r.user_id, r.relation, p.display_name, p.picture_url
               FROM parent_student_relations r
               LEFT JOIN parent_profiles p ON p.user_id = r.user_id
               WHERE r.student_id = ?
               ORDER BY r.relation''',
            (student_id,)
        ).fetchall()

        guardians = []
        for row in rows:
            guardians.append({
                'userId':      row['user_id'],
                'relation':    row['relation'],
                'label':       RELATION_LABELS.get(row['relation'], row['relation']),
                'displayName': row['display_name'] or '未知',
                'pictureUrl':  f"/api/parents/{row['user_id']}/avatar" if row['picture_url'] else None,
            })
        return jsonify(guardians), 200
    finally:
        conn.close()


@parent_bp.route('/<user_id>/relation/<student_id>', methods=['GET'])
def get_my_relation(user_id, student_id):
    """Return this user's current relation to a student (or null)."""
    conn = data_service.get_db()
    try:
        _ensure_relations_table(conn)
        row = conn.execute(
            'SELECT relation FROM parent_student_relations WHERE user_id = ? AND student_id = ?',
            (user_id, student_id)
        ).fetchone()
        if row:
            rel = row['relation']
            return jsonify({'relation': rel, 'label': RELATION_LABELS.get(rel, rel)}), 200
        return jsonify({'relation': None, 'label': None}), 200
    finally:
        conn.close()

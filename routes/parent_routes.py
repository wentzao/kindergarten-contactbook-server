from flask import Blueprint, Response, jsonify, request
from services.data_service import DataService
from datetime import datetime
import os

parent_bp = Blueprint('parent', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)


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


@parent_bp.route('/<user_id>/profile', methods=['POST'])
def save_profile(user_id):
    """Upsert a parent's LINE profile and optionally record a student binding.

    Body JSON: {
        "display_name": "...",
        "picture_url": "...",
        "student_id": "..."   <- optional; when provided, registers this parent
                                 as a guardian of that student
    }
    Called automatically on every app login, and again when binding a student.
    """
    data = request.get_json(silent=True) or {}
    display_name = (data.get('display_name') or '').strip()
    picture_url  = (data.get('picture_url')  or '').strip()
    student_id   = (data.get('student_id')   or '').strip()

    # Fetch the LINE avatar blob so we own a copy of it
    picture_data = None
    picture_mime = 'image/jpeg'
    if picture_url:
        try:
            from urllib import request as urllib_req
            req = urllib_req.Request(picture_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib_req.urlopen(req, timeout=10) as resp:
                picture_data = resp.read()
                ct = resp.headers.get('Content-Type', 'image/jpeg')
                picture_mime = ct.split(';')[0].strip()
        except Exception as e:
            print(f'[profile] Failed to fetch avatar for {user_id}: {e}')

    conn = data_service.get_db()
    try:
        now_iso = datetime.now().isoformat()

        # Upsert parent profile
        if picture_data:
            conn.execute('''
                INSERT INTO parent_profiles
                    (user_id, display_name, picture_url, picture_data, picture_mime, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    picture_url  = excluded.picture_url,
                    picture_data = excluded.picture_data,
                    picture_mime = excluded.picture_mime,
                    updated_at   = excluded.updated_at
            ''', (user_id, display_name, picture_url, picture_data, picture_mime, now_iso))
        else:
            conn.execute('''
                INSERT INTO parent_profiles (user_id, display_name, picture_url, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    picture_url  = excluded.picture_url,
                    updated_at   = excluded.updated_at
            ''', (user_id, display_name, picture_url, now_iso))

        # Record the guardian → student binding if a student_id was provided
        if student_id:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS student_bindings (
                    user_id    VARCHAR(100) NOT NULL,
                    student_id VARCHAR(50)  NOT NULL,
                    created_at VARCHAR(50),
                    PRIMARY KEY (user_id, student_id)
                )
            ''')
            conn.execute('''
                INSERT OR IGNORE INTO student_bindings (user_id, student_id, created_at)
                VALUES (?, ?, ?)
            ''', (user_id, student_id, now_iso))

        conn.commit()
        return jsonify({'ok': True}), 200
    finally:
        conn.close()


@parent_bp.route('/student/<student_id>/guardians', methods=['GET'])
def get_student_guardians(student_id):
    """Return all parents bound to a student with their LINE profile info."""
    conn = data_service.get_db()
    try:
        # Ensure table exists (created lazily on first bind)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS student_bindings (
                user_id    VARCHAR(100) NOT NULL,
                student_id VARCHAR(50)  NOT NULL,
                created_at VARCHAR(50),
                PRIMARY KEY (user_id, student_id)
            )
        ''')

        rows = conn.execute(
            '''SELECT b.user_id, p.display_name, p.picture_url
               FROM student_bindings b
               LEFT JOIN parent_profiles p ON p.user_id = b.user_id
               WHERE b.student_id = ?
               ORDER BY b.created_at''',
            (student_id,)
        ).fetchall()

        guardians = []
        for row in rows:
            guardians.append({
                'userId':      row['user_id'],
                'displayName': row['display_name'] or '未知',
                'pictureUrl':  f"/api/parents/{row['user_id']}/avatar" if row['picture_url'] else None,
            })
        return jsonify(guardians), 200
    finally:
        conn.close()

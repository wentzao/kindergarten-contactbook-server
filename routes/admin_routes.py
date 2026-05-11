"""
Admin endpoints — destructive operations protected by X-Admin-Token.

Currently exposes:
  GET    /api/admin/student/<id>/footprint   Dry-run: list everything that
                                              would be removed for a student.
  DELETE /api/admin/student/<id>             Wipe a student from DB + image
                                              server. Used when a student
                                              graduates / withdraws.

This blueprint orchestrates cleanup across both the SQLite DB and the external
image server. The image server itself stays domain-agnostic — it only knows how
to delete folders by path.
"""
import os
import sqlite3
from flask import Blueprint, jsonify, request
import requests
from services.admin_auth import require_admin

admin_bp = Blueprint('admin', __name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'kindergarten.db')
IMAGE_SERVER_URL = os.environ.get('IMAGE_SERVER_URL', 'https://imageserver.wentzao.com').rstrip('/')
IMAGE_SERVER_ADMIN_TOKEN = os.environ.get('IMAGE_SERVER_ADMIN_TOKEN', '').strip()


# Tables that store rows keyed by student/child. Order matters only when there
# are foreign-key constraints (we have none in current schema, but we still
# delete child tables before parent for cleanliness).
#
# Each entry: (table_name, column_name)
STUDENT_TABLES = [
    ('contact_books',         'student_id'),
    ('leave_records',         'child_id'),
    ('med_records',           'child_id'),
    ('survey_responses',      'child_id'),
    ('teacher_comment_reads', 'student_id'),
    ('student_names',         'student_id'),
    ('student_bindings',      'student_id'),
    ('students',              'student_id'),  # parent row — last
]

# Image server folders that contain per-student data, relative to the bucket
# root. {sid} is replaced with the actual student id.
#
# Adding a new feature with per-student images? Append its folder here so that
# `delete student` continues to clean it up.
STUDENT_IMAGE_FOLDERS = [
    'contactbook/{sid}',    # chat images (existing)
    'meds/{sid}',           # meds: prescriptions + signatures
    'leave/{sid}',          # leave: signatures
    'contact-book/{sid}',   # daily contact book signatures (future)
]


def _get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _count_db_rows(conn, student_id):
    """Return dict of {table_name: row_count} for this student across all tables."""
    counts = {}
    for table, col in STUDENT_TABLES:
        try:
            row = conn.execute(
                f'SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?',
                (student_id,),
            ).fetchone()
            counts[table] = row['n'] if row else 0
        except sqlite3.OperationalError:
            # Table might not exist yet (e.g. dynamic tables created on first use)
            counts[table] = 0
    return counts


def _image_server_stats(student_id):
    """Hit /admin/api/folder-stats for each known student folder. Returns dict and total bytes."""
    if not IMAGE_SERVER_ADMIN_TOKEN:
        return None, 0  # not configured
    headers = {'X-Admin-Token': IMAGE_SERVER_ADMIN_TOKEN}
    folders = {}
    total_bytes = 0
    for tmpl in STUDENT_IMAGE_FOLDERS:
        path = tmpl.format(sid=student_id)
        try:
            r = requests.get(
                f'{IMAGE_SERVER_URL}/admin/api/folder-stats',
                params={'path': path},
                headers=headers,
                timeout=10,
            )
            if r.ok:
                data = r.json()
                folders[path] = {
                    'exists': data.get('exists', False),
                    'count': data.get('count', 0),
                    'size_bytes': data.get('size_bytes', 0),
                }
                total_bytes += data.get('size_bytes', 0)
            else:
                folders[path] = {'error': f'HTTP {r.status_code}'}
        except requests.RequestException as e:
            folders[path] = {'error': str(e)}
    return folders, total_bytes


def _delete_image_folders(student_id):
    """Hit /admin/api/delete-folder for each known student folder."""
    if not IMAGE_SERVER_ADMIN_TOKEN:
        return {'_skipped': 'IMAGE_SERVER_ADMIN_TOKEN not configured'}
    headers = {
        'X-Admin-Token': IMAGE_SERVER_ADMIN_TOKEN,
        'Content-Type': 'application/json',
    }
    results = {}
    for tmpl in STUDENT_IMAGE_FOLDERS:
        path = tmpl.format(sid=student_id)
        try:
            r = requests.post(
                f'{IMAGE_SERVER_URL}/admin/api/delete-folder',
                json={'path': path},
                headers=headers,
                timeout=30,
            )
            if r.ok:
                data = r.json()
                results[path] = {
                    'deleted_count': data.get('deleted_count', 0),
                    'size_bytes': data.get('size_bytes', 0),
                }
            elif r.status_code == 404:
                # Folder didn't exist — nothing to clean, not an error
                results[path] = {'deleted_count': 0, 'size_bytes': 0, 'note': 'not found'}
            else:
                results[path] = {'error': f'HTTP {r.status_code}: {r.text[:200]}'}
        except requests.RequestException as e:
            results[path] = {'error': str(e)}
    return results


# ── Endpoints ─────────────────────────────────────────────────────────────────

@admin_bp.route('/student/<student_id>/footprint', methods=['GET'])
@require_admin
def student_footprint(student_id):
    """
    Dry-run preview: report everything that *would* be removed if we called
    DELETE on this student. Safe — does not modify state.
    """
    student_id = student_id.strip()
    if not student_id:
        return jsonify({'error': 'student_id is required'}), 400

    conn = _get_db()
    try:
        db_counts = _count_db_rows(conn, student_id)
    finally:
        conn.close()

    image_folders, total_bytes = _image_server_stats(student_id)

    db_row_total = sum(db_counts.values())

    return jsonify({
        'student_id': student_id,
        'db_rows': db_counts,
        'db_rows_total': db_row_total,
        'image_folders': image_folders,
        'total_image_size_bytes': total_bytes,
        'exists': db_row_total > 0,
    }), 200


@admin_bp.route('/student/<student_id>', methods=['DELETE'])
@require_admin
def delete_student(student_id):
    """
    Permanently remove a student from the database and clean up their image
    server folders. Intended for use after graduation / withdrawal.

    This is irreversible. The caller (student management web UI) is expected to
    call /footprint first and surface a confirmation step before invoking this.
    """
    student_id = student_id.strip()
    if not student_id:
        return jsonify({'error': 'student_id is required'}), 400

    # 1. DB cleanup — single transaction so we don't half-delete
    conn = _get_db()
    db_removed = {}
    try:
        cursor = conn.cursor()
        cursor.execute('BEGIN')
        for table, col in STUDENT_TABLES:
            try:
                res = cursor.execute(
                    f'DELETE FROM {table} WHERE {col} = ?',
                    (student_id,),
                )
                db_removed[table] = res.rowcount
            except sqlite3.OperationalError as e:
                # Table missing — record as 0
                db_removed[table] = 0
                print(f'[admin] DELETE {table}: skipped ({e})')
        conn.commit()
        print(f'[admin] DB cleanup for student {student_id}: {db_removed}')
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'error': f'DB transaction failed: {e}'}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 2. Image server cleanup — best-effort; failures here don't roll back the DB
    image_removed = _delete_image_folders(student_id)

    return jsonify({
        'student_id': student_id,
        'db_rows_removed': db_removed,
        'image_folders_removed': image_removed,
    }), 200

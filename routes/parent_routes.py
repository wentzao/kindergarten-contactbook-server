from flask import Blueprint, Response
from services.data_service import DataService
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

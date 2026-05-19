from flask import Flask, jsonify, request, send_from_directory, render_template
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import json
from datetime import datetime

# Initialize Flask App
app = Flask(__name__)
# Enable CORS for all routes
CORS(app, resources={r"/api/*": {
    "origins": ["https://teacher-contact-book.wentzao.com", "https://newsroom.wentzao.com", "http://localhost:5173", "http://localhost:4173", "http://localhost:5500", "http://127.0.0.1:5500"],
    "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    "allow_headers": ["Content-Type", "Authorization", "X-Admin-Token"],
    "supports_credentials": True
}})
# Fix headers for Nginx proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Configuration
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Import Routes
from routes.leave_routes import leave_bp
from routes.med_routes import med_bp
from routes.survey_routes import survey_bp
from routes.contact_book_routes import contact_book_bp, start_contact_book_background_workers
from routes.news_routes import news_bp
from routes.auth_routes import auth_bp
from routes.student_routes import student_bp
from routes.notification_routes import notification_bp
from routes.notification_grants_routes import grants_bp
from routes.class_journal_routes import journal_bp
from routes.lock_routes import lock_bp
from routes.parent_routes import parent_bp
from routes.collab_routes import collab_bp, collab_sock
from routes.admin_routes import admin_bp
from routes.teacher_request_routes import teacher_requests_bp

app.register_blueprint(leave_bp, url_prefix='/api/leave')
app.register_blueprint(med_bp, url_prefix='/api/meds')
app.register_blueprint(survey_bp, url_prefix='/api/survey')
app.register_blueprint(contact_book_bp, url_prefix='/api/contact-book')
app.register_blueprint(news_bp, url_prefix='/api/news')
app.register_blueprint(auth_bp, url_prefix='/api/auth')
app.register_blueprint(student_bp, url_prefix='/api/teacher')
app.register_blueprint(notification_bp, url_prefix='/api/notifications')
app.register_blueprint(grants_bp, url_prefix='/api/notifications/grants')
app.register_blueprint(journal_bp, url_prefix='/api/class-journal')
app.register_blueprint(lock_bp, url_prefix='/api/locks')
app.register_blueprint(parent_bp, url_prefix='/api/parents')
app.register_blueprint(collab_bp, url_prefix='/api/collab')
app.register_blueprint(admin_bp, url_prefix='/api/admin')
app.register_blueprint(teacher_requests_bp, url_prefix='/api/teacher-requests')
collab_sock.init_app(app)

# Drop legacy tables that are no longer used by the application
def _run_startup_migrations():
    import sqlite3
    db_path = (
        os.environ.get('KINDERGARTEN_DB_PATH')
        or os.environ.get('DB_PATH')
        or os.path.join(os.path.dirname(__file__), 'kindergarten.db')
    )
    try:
        timeout = float(os.environ.get('SQLITE_BUSY_TIMEOUT_SECONDS', '5'))
        conn = sqlite3.connect(db_path, timeout=timeout)
        conn.execute(f'PRAGMA busy_timeout={int(timeout * 1000)}')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('DROP TABLE IF EXISTS parent_student_relations')
        conn.commit()
        conn.close()
        print('[startup] Dropped legacy table parent_student_relations (if existed)')
    except Exception as e:
        print(f'[startup] Migration warning: {e}')

_run_startup_migrations()
start_contact_book_background_workers()

# Request logging middleware
@app.before_request
def log_request_info():
    if os.environ.get('REQUEST_LOGGING', '').lower() not in ('1', 'true', 'yes'):
        return
    print('=' * 80)
    print(f"[REQUEST] {request.method} {request.path}")
    print(f"[REQUEST] Remote Address: {request.remote_addr}")
    if request.args:
        print(f"[REQUEST] Query Params: {dict(request.args)}")
    print('=' * 80)

@app.route('/')
def index():
    return "Kindergarten Contact Book API Running"

# Photo viewer page (ported from photo_view_V5, served as standalone HTML for iframe embedding)
@app.route('/photo_view')
def photo_view():
    return render_template('photo_view.html')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB limit

def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if not _allowed_file(file.filename):
        return jsonify({'error': f'不允許的檔案類型，僅接受：{", ".join(ALLOWED_EXTENSIONS)}'}), 400

    from werkzeug.utils import secure_filename
    safe_name = secure_filename(file.filename)
    filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    file.save(filepath)

    return jsonify({'url': f"/static/uploads/{filename}"}), 200


if __name__ == '__main__':
    # Listen on all interfaces
    app.run(host='0.0.0.0', port=5000, debug=True)

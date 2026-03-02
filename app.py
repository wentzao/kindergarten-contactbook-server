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
    "origins": ["https://teacher-contact-book.wentzao.com", "http://localhost:5173", "http://localhost:4173"],
    "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    "allow_headers": ["Content-Type", "Authorization"],
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
from routes.contact_book_routes import contact_book_bp
from routes.news_routes import news_bp
from routes.auth_routes import auth_bp
from routes.student_routes import student_bp
from routes.notification_routes import notification_bp

app.register_blueprint(leave_bp, url_prefix='/api/leave')
app.register_blueprint(med_bp, url_prefix='/api/meds')
app.register_blueprint(survey_bp, url_prefix='/api/survey')
app.register_blueprint(contact_book_bp, url_prefix='/api/contact-book')
app.register_blueprint(news_bp, url_prefix='/api/news')
app.register_blueprint(auth_bp, url_prefix='/api/auth')
app.register_blueprint(student_bp, url_prefix='/api/teacher')
app.register_blueprint(notification_bp, url_prefix='/api/notifications')

# Request logging middleware
@app.before_request
def log_request_info():
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

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if file:
        filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        file.save(filepath)
        
        # Return full URL
        # For local dev, assuming IP access. In prod, use domain.
        # Here we just return relative path or handle absolute URL construct in frontend/service
        return jsonify({'url': f"/static/uploads/{filename}"}), 200

if __name__ == '__main__':
    # Listen on all interfaces
    app.run(host='0.0.0.0', port=5000, debug=True)

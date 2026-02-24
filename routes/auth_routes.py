from flask import Blueprint, request, jsonify
import requests
from datetime import datetime

auth_bp = Blueprint('auth_bp', __name__)

# 透過 web.wentzao.com 的 API 取得教師資料（取代直接讀取網路磁碟上的 JSON 檔案）
WEB_WENTZAO_TEACHER_AUTH_API = 'https://web.wentzao.com/api/get_teacher_for_auth'

@auth_bp.route('/teacher_login', methods=['POST'])
def teacher_login():
    data = request.json
    if not data or 'userId' not in data:
        return jsonify({'error': 'Missing userId'}), 400
    
    user_id = data.get('userId')
    
    try:
        # 向 web.wentzao.com 請求教師驗證資料
        resp = requests.post(
            WEB_WENTZAO_TEACHER_AUTH_API,
            json={'userId': user_id},
            timeout=10
        )
        
        if resp.status_code == 200:
            # 驗證成功，直接回傳 web.wentzao.com 給的資料
            return jsonify(resp.json()), 200
        elif resp.status_code == 403:
            return jsonify(resp.json()), 403
        elif resp.status_code == 404:
            return jsonify(resp.json()), 404
        else:
            return jsonify({'error': f'Upstream API error: {resp.status_code}'}), resp.status_code
            
    except requests.exceptions.Timeout:
        return jsonify({'error': 'web.wentzao.com API timeout'}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to web.wentzao.com'}), 502
    except Exception as e:
        return jsonify({'error': f'Failed to verify teacher: {str(e)}'}), 500

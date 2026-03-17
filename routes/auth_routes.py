from flask import Blueprint, request, jsonify, make_response
import requests
import os
import time
from datetime import datetime

auth_bp = Blueprint('auth_bp', __name__)

# 透過 web.wentzao.com 的 API 取得教師資料（取代直接讀取網路磁碟上的 JSON 檔案）
WEB_WENTZAO_TEACHER_AUTH_API = 'https://web.wentzao.com/api/get_teacher_for_auth'

# LINE Login Channel credentials (for PWA OAuth code exchange)
# 必須透過環境變數設定，不得有預設值。請在 .env 或伺服器環境中設定：
#   LINE_CHANNEL_ID=<your_channel_id>
#   LINE_CHANNEL_SECRET=<your_channel_secret>
LINE_CHANNEL_ID = os.environ.get('LINE_CHANNEL_ID') or ''
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET') or ''

if not LINE_CHANNEL_ID or not LINE_CHANNEL_SECRET:
    import sys
    print('[FATAL] LINE_CHANNEL_ID 或 LINE_CHANNEL_SECRET 環境變數未設定，LINE 登入功能將無法運作。', file=sys.stderr)

# ── Pending Logins Store (loginToken → {userData, timestamp}) ──
# 暫存 PWA 登入結果，5 分鐘過期
_pending_logins = {}
_PENDING_TTL = 300  # 5 minutes


def _cleanup_pending():
    """清除過期的 pending logins"""
    now = time.time()
    expired = [k for k, v in _pending_logins.items() if now - v['timestamp'] > _PENDING_TTL]
    for k in expired:
        del _pending_logins[k]


def _do_teacher_auth(user_id):
    """共用的教師驗證邏輯：向 web.wentzao.com 驗證教師身分，回傳 Flask response"""
    resp = requests.post(
        WEB_WENTZAO_TEACHER_AUTH_API,
        json={'userId': user_id},
        timeout=10
    )

    if resp.status_code == 200:
        return jsonify(resp.json()), 200
    elif resp.status_code == 403:
        return jsonify(resp.json()), 403
    elif resp.status_code == 404:
        return jsonify(resp.json()), 404
    else:
        return jsonify({'error': f'Upstream API error: {resp.status_code}'}), resp.status_code


def _do_teacher_auth_raw(user_id):
    """教師驗證，回傳 (data_dict, status_code) 供 callback 使用"""
    resp = requests.post(
        WEB_WENTZAO_TEACHER_AUTH_API,
        json={'userId': user_id},
        timeout=10
    )
    return resp.json(), resp.status_code


@auth_bp.route('/teacher_login', methods=['POST'])
def teacher_login():
    data = request.json
    if not data or 'userId' not in data:
        return jsonify({'error': 'Missing userId'}), 400

    user_id = data.get('userId')

    try:
        return _do_teacher_auth(user_id)
    except requests.exceptions.Timeout:
        return jsonify({'error': 'web.wentzao.com API timeout'}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot connect to web.wentzao.com'}), 502
    except Exception as e:
        return jsonify({'error': f'Failed to verify teacher: {str(e)}'}), 500


# ═══════════════════════════════════════════
#  PWA Server-Bridged LINE Login
# ═══════════════════════════════════════════

SUCCESS_HTML = '''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登入成功</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            min-height: 100vh;
            display: flex; align-items: center; justify-content: center;
            background: linear-gradient(135deg, #e8f5e9 0%%, #f1f8e9 100%%);
        }
        .card {
            background: white; border-radius: 20px;
            padding: 48px 32px; text-align: center;
            box-shadow: 0 4px 24px rgba(0,0,0,0.08);
            max-width: 360px; width: 90%%;
        }
        .icon { font-size: 64px; margin-bottom: 16px; }
        h1 { font-size: 22px; color: #2e7d32; margin-bottom: 8px; }
        p { font-size: 15px; color: #666; line-height: 1.6; margin-bottom: 8px; }
        .name { font-size: 18px; color: #333; font-weight: 600; margin: 12px 0; }
        .hint {
            font-size: 13px; color: #999;
            margin-top: 20px; padding-top: 16px;
            border-top: 1px solid #eee;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">✅</div>
        <h1>LINE 登入成功</h1>
        <p class="name">%(teacher_name)s 老師</p>
        <p>身分驗證已完成</p>
        <p class="hint">請回到主畫面，點選「教師聯絡簿」App 圖示即可開始使用</p>
    </div>
</body>
</html>'''

ERROR_HTML = '''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登入失敗</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            min-height: 100vh;
            display: flex; align-items: center; justify-content: center;
            background: linear-gradient(135deg, #fce4ec 0%%, #fff3e0 100%%);
        }
        .card {
            background: white; border-radius: 20px;
            padding: 48px 32px; text-align: center;
            box-shadow: 0 4px 24px rgba(0,0,0,0.08);
            max-width: 360px; width: 90%%;
        }
        .icon { font-size: 64px; margin-bottom: 16px; }
        h1 { font-size: 22px; color: #c62828; margin-bottom: 8px; }
        p { font-size: 15px; color: #666; line-height: 1.6; }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">❌</div>
        <h1>登入失敗</h1>
        <p>%(error)s</p>
        <p style="margin-top:16px;font-size:13px;color:#999;">請回到 App 重新嘗試登入</p>
    </div>
</body>
</html>'''


@auth_bp.route('/line_callback', methods=['GET'])
def line_callback():
    """LINE OAuth callback — LINE 登入完成後重導向至此"""
    code = request.args.get('code')
    state = request.args.get('state')  # This is the loginToken from PWA
    error = request.args.get('error')

    if error:
        html = ERROR_HTML % {'error': f'LINE 拒絕授權: {error}'}
        resp = make_response(html, 200)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp

    if not code or not state:
        html = ERROR_HTML % {'error': '缺少必要參數'}
        resp = make_response(html, 200)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp

    try:
        # Step 1: 用 authorization code 換取 access token
        redirect_uri = request.url_root.rstrip('/') + '/api/auth/line_callback'
        token_resp = requests.post(
            'https://api.line.me/oauth2/v2.1/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': redirect_uri,
                'client_id': LINE_CHANNEL_ID,
                'client_secret': LINE_CHANNEL_SECRET,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=10
        )

        if token_resp.status_code != 200:
            print(f'[Auth] LINE token exchange failed: {token_resp.status_code} {token_resp.text}')
            html = ERROR_HTML % {'error': 'LINE 授權碼交換失敗，請重試'}
            resp = make_response(html, 200)
            resp.headers['Content-Type'] = 'text/html; charset=utf-8'
            return resp

        access_token = token_resp.json().get('access_token')

        # Step 2: 用 access token 取得使用者 profile
        profile_resp = requests.get(
            'https://api.line.me/v2/profile',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10
        )

        if profile_resp.status_code != 200:
            html = ERROR_HTML % {'error': '無法取得 LINE 用戶資料'}
            resp = make_response(html, 200)
            resp.headers['Content-Type'] = 'text/html; charset=utf-8'
            return resp

        user_id = profile_resp.json().get('userId')
        print(f'[Auth] PWA LINE callback: userId={user_id}')

        # Step 3: 驗證教師身分
        teacher_data, status = _do_teacher_auth_raw(user_id)

        if status != 200:
            err_msg = teacher_data.get('error', '您的帳號不是已註冊的教師')
            html = ERROR_HTML % {'error': err_msg}
            resp = make_response(html, 200)
            resp.headers['Content-Type'] = 'text/html; charset=utf-8'
            return resp

        # Step 4: 儲存登入結果，讓 PWA 之後可以取用
        _cleanup_pending()
        _pending_logins[state] = {
            'userData': teacher_data,
            'timestamp': time.time()
        }
        print(f'[Auth] PWA login stored for token: {state[:8]}...')

        # Step 5: 顯示成功頁面
        teacher_name = teacher_data.get('name', '')
        html = SUCCESS_HTML % {'teacher_name': teacher_name}
        resp = make_response(html, 200)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp

    except Exception as e:
        print(f'[Auth] LINE callback error: {str(e)}')
        html = ERROR_HTML % {'error': f'登入過程發生錯誤'}
        resp = make_response(html, 200)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp


@auth_bp.route('/check_login', methods=['POST'])
def check_login():
    """PWA 呼叫此端點確認登入是否已完成"""
    data = request.json
    if not data or 'loginToken' not in data:
        return jsonify({'error': 'Missing loginToken'}), 400

    token = data['loginToken']
    _cleanup_pending()

    if token in _pending_logins:
        result = _pending_logins.pop(token)
        return jsonify(result['userData']), 200
    else:
        return jsonify({'error': 'Login not completed yet'}), 404

# 技術債務清單（Tech Debt）— 後端 API

> 最後更新：2026-03-17
> 本文件記錄後端 codebase review 中發現的設計問題、重複程式碼、潛在風險等，供後續重構參考。

---

## 目錄

1. [安全疑慮](#1-安全疑慮)
2. [架構 / 設計問題](#2-架構--設計問題)
3. [重複程式碼（DRY 違反）](#3-重複程式碼dry-違反)
4. [效能疑慮](#4-效能疑慮)
5. [程式碼品質問題](#5-程式碼品質問題)

---

## 1. 安全疑慮

### 1-A. LINE Channel Secret 硬編碼為 fallback 預設值

- **位置**：`routes/auth_routes.py:13-14`
- **問題**：
  ```python
  LINE_CHANNEL_ID = os.environ.get('LINE_CHANNEL_ID', '1655533540')
  LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '3013918a5f553adfedcb20335e42182e')
  ```
  `LINE_CHANNEL_SECRET` 是真實的 OAuth Client Secret，不應以任何形式出現在原始碼中。即使透過 `os.environ.get` 讀取，fallback 值直接寫在程式碼裡，任何能讀取 git history 的人都能取得它。
- **建議修法**：
  1. 移除 fallback 預設值，改為 `os.environ.get('LINE_CHANNEL_SECRET')` 或 `os.environ['LINE_CHANNEL_SECRET']`
  2. 若環境變數未設定，應在啟動時明確報錯（fail fast）
  3. 若 secret 已外洩，立即在 LINE Developers Console 重新產生
  4. 使用 `git filter-branch` 或 BFG Repo Cleaner 從 git history 清除
- **優先級**：🔴 高（已修復）

---

### 1-B. 所有 API 端點完全沒有身份驗證中介層

- **位置**：`app.py`、`routes/*.py`（全部）
- **問題**：Flask app 沒有任何全域 auth middleware。家長端所有資料（聯絡簿、請假、用藥、留言）任何人只要知道端點與學生 ID 都能直接存取或竄改。目前後端完全信任 client 傳來的 ID。

  對比：`student_routes.py` 裡有呼叫 `web.wentzao.com` 驗證教師身份，但 `contact_book_routes.py`、`leave_routes.py`、`med_routes.py` 等完全沒有驗證。
- **建議修法**：
  1. 建立 `middleware/auth.py`，實作 `require_auth` decorator
  2. 家長端 API 驗證 LINE access token（向 `api.line.me/v2/profile` 驗證）
  3. 教師端 API 驗證教師 LINE token + 呼叫 `web.wentzao.com` 確認身份
  4. 在 `app.py` 設定各 blueprint 的 `before_request` 驗證鉤子
- **優先級**：🔴 高

---

### 1-C. /api/upload 無檔案類型驗證（任意檔案上傳）

- **位置**：`app.py:63-79`
- **問題**：
  ```python
  if file:
      filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}"
      filepath = os.path.join(UPLOAD_DIR, filename)
      file.save(filepath)
  ```
  - 沒有驗證 MIME type（可上傳 .php、.py、.sh 等危險檔案）
  - `file.filename` 未做 sanitize，路徑穿越攻擊（path traversal）風險
  - 沒有檔案大小限制
  - 儲存在 `static/uploads/`，可直接透過 URL 存取執行
- **建議修法**：
  1. 使用 `werkzeug.utils.secure_filename()` sanitize 檔名
  2. 白名單驗證副檔名（`{'png', 'jpg', 'jpeg', 'gif', 'webp'}`）
  3. 設定 `app.config['MAX_CONTENT_LENGTH']`（例如 10MB）
  4. 考慮驗證 MIME type（用 `python-magic`）
- **優先級**：🔴 高（已修復）

---

### 1-D. debug=True 可能用於生產環境

- **位置**：`app.py:83`
- **問題**：`app.run(debug=True)` 在生產環境會啟用 Werkzeug debugger，允許遠端執行任意 Python 程式碼。
- **建議修法**：從環境變數讀取：`debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'`，或透過 gunicorn 啟動（不用 `app.run()`）。
- **優先級**：🟡 中

---

### 1-E. kindergarten.db 未被 .gitignore 排除

- **位置**：`.gitignore:11`（`data/*.db`，但 `kindergarten.db` 在根目錄）
- **問題**：`.gitignore` 只排除 `data/` 子目錄下的 `.db` 檔案，根目錄的 `kindergarten.db` 可能已進版控。資料庫包含學生、家長個資、留言記錄等敏感資料。
- **建議修法**：在 `.gitignore` 加上 `*.db`（已修復）。確認 db 是否已在 git history 中，若有則用 BFG 清除。
- **優先級**：🔴 高（已修復）

---

## 2. 架構 / 設計問題

### 2-A. 登入狀態存在記憶體 dict（不可水平擴展）

- **位置**：`routes/auth_routes.py:18-19`
- **問題**：
  ```python
  _pending_logins = {}  # 記憶體中的暫存登入狀態
  ```
  - 伺服器重啟後所有進行中的登入流程失效
  - 多個 Gunicorn worker 進程各自有獨立的 dict，無法共享
  - 無法水平擴展到多台伺服器
- **建議修法**：改用 Redis 或 SQLite 暫存 pending logins（TTL 5 分鐘），確保跨進程可見。
- **優先級**：🟡 中

---

### 2-B. data/ 目錄的 JSON 檔案與 SQLite 並存，職責不清

- **位置**：`data/` 目錄、`services/data_service.py`
- **問題**：`data/` 目錄（舊版 JSON 儲存）仍存在，`data_service.py` 的 `DataService` 接受 `DATA_DIR` 參數但大部分操作已改用 SQLite。新開發者難以判斷資料的真正來源。
- **建議修法**：確認 JSON 資料已完整遷移後，移除 `data/` 目錄或改名為 `data_backup/`，並在 `data_service.py` 移除 `DATA_DIR` 參數。
- **優先級**：🟡 中

---

### 2-C. Auto-migration 直接在 module 載入時執行 DDL

- **位置**：`routes/contact_book_routes.py:13-26`
- **問題**：
  ```python
  def _auto_migrate():
      # ALTER TABLE ...
  _auto_migrate()  # 每次 import 時執行
  ```
  每次模組載入（含測試、reload）都會執行 `PRAGMA` 和可能的 `ALTER TABLE`，難以控制且在多 worker 部署時可能產生競爭條件。
- **建議修法**：將所有 migration 邏輯集中到 `init_db.py` 或 `migrate_db.py`，部署時手動執行一次，不在 route 載入時自動執行。
- **優先級**：🟡 中

---

### 2-D. DataService 與 route 檔直接存取 DB 混用

- **位置**：`routes/leave_routes.py`、`routes/contact_book_routes.py` 等
- **問題**：部分資料操作透過 `DataService` 類別，部分則在 route 函式內直接呼叫 `conn.execute()`（例如 `leave_routes.py` 的 `get_today_leaves`、`get_month_leaves`）。無一致的資料存取層，維護困難。
- **建議修法**：將所有 SQL 查詢集中到 `data_service.py`，route 函式只負責 HTTP request/response 處理。
- **優先級**：🟡 中

---

### 2-E. 部分 route 重複登記兩個路徑解決尾斜線問題

- **位置**：`routes/leave_routes.py:69-70`
- **問題**：
  ```python
  @leave_bp.route('', methods=['POST'])
  @leave_bp.route('/', methods=['POST'])
  def submit_leave_request():
  ```
  用雙重裝飾器處理 trailing slash，應改用 Flask 設定 `strict_slashes=False`。
- **建議修法**：在 `app.py` 加上 `app.url_map.strict_slashes = False` 或在 Blueprint 建立時設定。
- **優先級**：🟢 低

---

## 3. 重複程式碼（DRY 違反）

### 3-A. 每個 route 檔各自建立 DataService 實例

- **位置**：`routes/leave_routes.py:8`、`routes/contact_book_routes.py:10`、`routes/med_routes.py`（同樣模式）
- **問題**：
  ```python
  DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
  data_service = DataService(DATA_DIR)
  ```
  每個 blueprint 各自建立一個 `DataService`，重複取 `DATA_DIR` 路徑，違反 DRY 原則。
- **建議修法**：在 `app.py` 建立單一 `data_service` 實例，透過 Flask 的 `app.extensions` 或 dependency injection 傳入各 blueprint。
- **優先級**：🟡 中

---

### 3-B. `load_json()` 同樣的 helper 在多處重複

- **位置**：`routes/contact_book_routes.py:47-53`、可能在其他 route 也有類似邏輯
- **問題**：JSON 欄位反序列化的 try/except 邏輯散落在程式碼中。
- **建議修法**：移入 `services/data_service.py` 或 `utils/json_utils.py` 統一管理。
- **優先級**：🟢 低

---

### 3-C. `_get_notifier()` / `_get_status_notifier()` 重複的 lazy import 模式

- **位置**：`routes/contact_book_routes.py:29-44`
- **問題**：兩個幾乎相同的函式，只差在 import 的目標函式名稱不同。
- **建議修法**：合併為一個通用的 `_get_notification_fn(fn_name)` helper，或直接在模組頂層 import（已有 try/except 處理）。
- **優先級**：🟢 低

---

## 4. 效能疑慮

### 4-A. 每次 request 的 logging 使用同步 print() 到 stdout

- **位置**：`app.py:45-52`（before_request 中介層）、所有 route 函式
- **問題**：大量 `print()` 呼叫（含 request body、headers）在每次 request 執行，在高流量下會有 I/O blocking。目前的 debug print 包括完整的 request raw data，不適合生產環境。
- **建議修法**：改用 Python `logging` 模組，設定 log level（DEBUG/INFO/WARNING/ERROR），生產環境只輸出 WARNING 以上。
- **優先級**：🟡 中

---

### 4-B. 每次請求都開啟新的 SQLite 連線

- **位置**：`services/data_service.py`（`get_db()`）、各 route 函式
- **問題**：SQLite 的每次 `get_db()` 都開一個新的連線，request 結束後手動 `conn.close()`。Flask 有 `g` 機制可以管理 per-request 資源，目前沒有使用。
- **建議修法**：使用 Flask `g` 物件管理連線生命週期，或改用 SQLAlchemy 連線池。長遠考慮從 SQLite 遷移至 PostgreSQL（支援並發寫入）。
- **優先級**：🟡 中

---

## 5. 程式碼品質問題

### 5-A. leave_routes.py 的 submit_leave_request 有大量 debug print

- **位置**：`routes/leave_routes.py:73-92`
- **問題**：POST 路由打印所有 headers、raw data、parsed JSON，明顯是開發時的 debug 輸出，不應留在生產程式碼中。個資（家長、學生資料）會被 print 到 stdout/log。
- **建議修法**：移除或改為 `logger.debug()` 層級，生產環境預設關閉。
- **優先級**：🟡 中

---

### 5-B. auth_routes.py 中 HTML 模板硬編碼在 Python 字串

- **位置**：`routes/auth_routes.py:80-155`（SUCCESS_HTML、ERROR_HTML）
- **問題**：兩段完整的 HTML 以多行字串寫在 Python 檔中，維護困難，且使用 `%` 格式化而非 Jinja2 模板（不能自動 escape，有 XSS 風險）。
- **建議修法**：移入 `templates/auth_success.html`、`templates/auth_error.html`，使用 `render_template()` 並讓 Jinja2 自動 escape 變數。
- **優先級**：🟡 中

---

### 5-C. 裸 except 掩蓋錯誤

- **位置**：`routes/contact_book_routes.py:52`
- **問題**：
  ```python
  except:
      return None
  ```
  空的 `except:` 會捕捉所有例外（包含 `KeyboardInterrupt`、`SystemExit`），讓錯誤完全靜默。
- **建議修法**：改為 `except (json.JSONDecodeError, TypeError, ValueError)`，或至少 `except Exception`。
- **優先級**：🟢 低

---

## 優先級總覽

| 編號 | 問題 | 優先級 | 狀態 |
|------|------|--------|------|
| 1-A | LINE Channel Secret 硬編碼 | 🔴 高 | ✅ 已修復 |
| 1-B | 所有 API 無身份驗證 | 🔴 高 | 待處理 |
| 1-C | /api/upload 無檔案驗證 | 🔴 高 | ✅ 已修復 |
| 1-D | debug=True 可能用於生產 | 🟡 中 | 待處理 |
| 1-E | kindergarten.db 未排除於 .gitignore | 🔴 高 | ✅ 已修復 |
| 2-A | 登入狀態存在記憶體 | 🟡 中 | 待處理 |
| 2-B | JSON / SQLite 並存職責不清 | 🟡 中 | 待處理 |
| 2-C | Auto-migration 在 module 載入時執行 | 🟡 中 | 待處理 |
| 2-D | DataService / 直接 SQL 混用 | 🟡 中 | 待處理 |
| 2-E | 雙重路由裝飾器 | 🟢 低 | 待處理 |
| 3-A | 每個 route 各自建立 DataService | 🟡 中 | 待處理 |
| 3-B | load_json() 重複 | 🟢 低 | 待處理 |
| 3-C | lazy import 模式重複 | 🟢 低 | 待處理 |
| 4-A | print() 同步 logging | 🟡 中 | 待處理 |
| 4-B | 每次 request 新開 SQLite 連線 | 🟡 中 | 待處理 |
| 5-A | submit_leave_request debug print | 🟡 中 | 待處理 |
| 5-B | HTML 模板寫在 Python 字串 | 🟡 中 | 待處理 |
| 5-C | 裸 except | 🟢 低 | 待處理 |

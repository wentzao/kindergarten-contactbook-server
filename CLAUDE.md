# CLAUDE.md — 文藻幼兒園聯絡簿後台（Flask API）

> 本檔案為 Claude Code 的專案說明，提供 AI 助理快速理解此專案的必要背景。

---

## 專案概述

這是「文藻幼兒園聯絡簿系統」的後端 API 服務，以 **Python Flask** 開發，提供 RESTful API 給：

- **家長端 App**（React Native，`contact-book/`）
- **教師端 PWA**（Vue.js，`kindergarten-contactbook-teacher-web/`）

主要功能：

- **LINE OAuth** 教師登入（透過外部 `web.wentzao.com` 驗證身份）
- **聯絡簿** CRUD（教師填寫 → 家長閱讀 → 簽名確認）
- **請假 / 用藥申請**管理
- **問卷調查**（定義、發放、填答收集）
- **校園公告**（含圖文、分班篩選、置頂）
- **FCM 推播通知**（新留言、聯絡簿更新）
- **學生班級資訊代理**（proxy 到 `student.wentzao.com`）

---

## 技術棧

| 類別 | 技術 |
|------|------|
| 語言 | Python 3 |
| 框架 | Flask |
| 資料庫 | SQLite（`kindergarten.db`） |
| 推播 | Firebase Cloud Messaging（HTTP v1 API） |
| 認證 | LINE OAuth 2.0（PKCE + backend callback） |
| 部署 | Nginx 反向代理 + Gunicorn（ProxyFix 已設定） |
| CORS | flask-cors（允許教師 PWA 與 localhost 開發） |

---

## 目錄結構

```
kindergarten-contactbook/
├── app.py                       # Flask 入口、Blueprint 註冊、CORS、上傳路由
├── schema.sql                   # SQLite 資料表定義
├── init_db.py                   # 初始化資料庫（執行 schema.sql）
├── migrate_db.py                # JSON → SQLite 資料遷移腳本
├── cleanup_broken_image.py      # 清除損毀的 Firebase 圖片 URL
├── requirements.txt             # Python 依賴
├── firebase-service-account.json# Firebase Admin SDK 金鑰（⚠️ 不應進版控）
├── routes/                      # API Blueprint（每個功能一個模組）
│   ├── auth_routes.py           # LINE OAuth 教師登入
│   ├── contact_book_routes.py   # 聯絡簿 CRUD（核心）
│   ├── leave_routes.py          # 請假記錄
│   ├── med_routes.py            # 用藥記錄
│   ├── news_routes.py           # 公告管理
│   ├── survey_routes.py         # 問卷調查
│   ├── notification_routes.py   # Push token、偏好設定、教師通知 inbox、未讀留言
│   └── student_routes.py        # 教師班級/學生資訊代理
├── services/
│   ├── data_service.py          # SQLite 資料存取抽象層
│   ├── send_notification.py     # FCM/Expo/APNs 推播發送
│   └── teacher_notification_store.py # 教師端通知 inbox 持久化
├── templates/
│   └── photo_view.html          # 相片檢視 HTML 頁面
├── static/                      # 靜態資源與上傳檔案
│   └── uploads/                 # 上傳圖片存放位置
├── data/                        # ⚠️ 舊版 JSON 儲存（已遷移至 SQLite，保留備用）
│   ├── students/                # 學生個別資料
│   ├── news/                    # 公告 JSON
│   └── surveys/                 # 問卷 JSON
└── contact-book-database-design.md # 資料庫設計說明（中文）
```

---

## 常用指令

```bash
# 安裝依賴
pip install -r requirements.txt

# 初始化資料庫（第一次執行）
python init_db.py

# 啟動開發伺服器
python app.py
# → 監聽 http://0.0.0.0:5000

# 從舊版 JSON 遷移資料至 SQLite
python migrate_db.py

# 清除損毀圖片 URL
python cleanup_broken_image.py

# 執行基本 API 測試
python test_api.py
```

---

## 資料庫結構（SQLite）

| 資料表 | 用途 |
|--------|------|
| `students` | 學生基本資料、監護人 LINE userId（JSON） |
| `contact_books` | 聯絡簿月份記錄（狀態、老師/家長填寫、留言） |
| `leave_records` | 請假記錄 |
| `med_records` | 用藥記錄 |
| `news` | 校園公告 |
| `surveys` | 問卷定義 |
| `survey_responses` | 問卷填答結果 |
| `push_tokens` | FCM/Expo/APNs 裝置 token |
| `notification_preferences` | 使用者通知偏好 |
| `teacher_notifications` | 教師端通知 inbox、已讀與 badge 計數 |
| `teacher_comment_reads` | 教師已讀留言追蹤 |
| `scheduled_contact_book_notifications` | 單一學生聯絡簿放學發布排程 |

---

## 重要外部依賴

| 服務 | URL | 用途 |
|------|-----|------|
| 教師身份驗證 | `web.wentzao.com` | 驗證 LINE userId 是否為教師 |
| 學生系統 | `student.wentzao.com` | 學生照片、Google Drive API Key |
| Firebase | `firebasestorage.app` | 家長上傳圖片、FCM 推播 |
| LINE OAuth | `api.line.me` | 教師登入授權 |

---

## 認證流程（教師）

1. 教師 PWA 導向 LINE 授權頁（含 `state` 參數）
2. LINE 回呼 `/api/auth/line_callback`
3. 後端交換 `access_token`，取得 LINE userId
4. 呼叫 `web.wentzao.com/api/get_teacher_for_auth` 驗證教師身份
5. 登入結果存入 **記憶體** dict（TTL 5 分鐘）⚠️
6. PWA 以 `state` 輪詢 `/api/auth/check_login` 取回憑證

> ⚠️ 目前 pending_logins 存在記憶體中，伺服器重啟後遺失。詳見 tech-debt。

---

## 聯絡簿狀態流

```
pending_teacher  →  pending_parent  →  read  →  signed
  (教師待填)         (家長待閱)       (已讀)   (已簽名)
```

---

## 程式碼慣例

### Blueprint 命名
- 每個功能域一個 Blueprint，統一以 `xxx_routes.py` 命名
- URL 前綴在 `app.py` 統一設定

### 資料庫存取
- 簡單資料操作透過 `data_service.py` 的 `DataService` 類別
- 複雜查詢或特定功能的 SQL 直接在 route 檔中撰寫（待整合）
- 連線使用 `sqlite3.Row` 讓欄位可用名稱存取

### JSON 回應
- 成功回傳 `jsonify(data)` + 適當 HTTP status code
- 錯誤回傳 `jsonify({'error': 'message'})` + 4xx/5xx status code

### 環境設定
- LINE OAuth 相關 credential 應從環境變數讀取
- Firebase 服務帳號從 `firebase-service-account.json` 讀取（⚠️ 見 tech-debt）

### 通知觸發
- 在 route handler 完成資料儲存後，用 `threading.Thread` 非同步發送通知
- 避免推播延遲影響 API 回應時間

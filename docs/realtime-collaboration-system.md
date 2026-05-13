# Realtime Collaboration Editing Rules — v3 草案

> 版本：v3 draft（2026-04-26）
> 適用範圍：後端 API、教師 Web、WenTzaoConnect Swift App
> 目標：把目前「一人編輯、其他人鎖定」改成類 Google Docs 的多人即時協作。

---

## 1. 現有 v2 機制研究結論

目前系統是悲觀鎖，不是協作編輯。

關鍵檔案：

| 端 | 檔案 | 目前責任 |
|---|---|---|
| Backend | `routes/lock_routes.py` | `/api/locks/*`，建立、釋放、heartbeat、查詢鎖 |
| Backend | `routes/class_journal_routes.py` | 班級日誌讀寫，使用 `lastUpdatedAt` 做 optimistic lock |
| Backend | `routes/contact_book_routes.py` | 學生備註 batch save，部分支援 `lastModified` conflict |
| Web | `src/pages/class-journal/ClassJournalEditor.jsx` | 班級日誌與學生備註的鎖定協調器、自動儲存 |
| Web | `src/components/StudentNoteCards.jsx` | 個別學生備註 focus/blur 鎖定處理 |
| Web | `src/services/api.js` | `lockService` HTTP client |
| iOS | `WenTzaoConnect/APIClient.swift` | 目前只有 class journal GET/PUT/DELETE |
| iOS | `WenTzaoConnect/ContentView.swift` | 班級日誌編輯 UI，目前沒有鎖定或即時協作 |

現有行為：

1. 鎖定標的是 `journal:{className}:{date}` 或 `note:{studentId}:{date}`。
2. Web 以 `localStorage` 產生 `web_{userId}_{browserId}`，同瀏覽器分頁共用，同一帳號不同裝置不同。
3. 後端以 `editing_locks.lock_owner_id` 判斷是否同一個持有者。
4. Web 持有鎖後每 150 秒 heartbeat；TTL 為 3 分鐘。
5. focus/input 時 acquire；blur、idle 8 秒、visibilitychange、beforeunload 時 release。
6. 鎖定變化用 FCM data message 通知其他教師端，其他端顯示 overlay。
7. 內容本身沒有逐字同步；只有 auto-save 後發 `data_updated`，其他端再重抓資料。

v2 不能達成的目標：

1. 多人同時在同一格輸入。
2. 遠端即時看到正在輸入的字。
3. 顯示遠端游標與選取範圍。
4. 離線或弱網路下安全合併兩端變更。

---

## 2. v3 核心決策

v3 不再把班級日誌與個人備註視為「需要互斥鎖的格子」，而是視為「多人可加入的協作文件」。

必須遵守：

1. 正常文字與區塊編輯不使用 `editing_locks` 阻擋其他人。
2. 鎖定只保留給破壞性動作或單次流程，例如刪除整份日誌、發布、批次清空。
3. 內容同步使用 CRDT 或等價的可合併操作紀錄，不可用 last-write-wins 覆蓋整份文件。
4. 使用者在線狀態、正在編輯哪個 block、游標位置是 presence，必須是暫存資料，不寫入正式內容。
5. 後端必須沿用現有資料表，讓家長端、舊 API 與既有備份流程不必一次改完。

建議技術選型：

| 選項 | 判斷 |
|---|---|
| Automerge | 優先。官方提供 JavaScript 與 Swift binding，適合 Web + iOS 共用 CRDT 模型。 |
| Yjs/Hocuspocus | Web 端成熟，awareness/cursor 很完整；但 Swift 端需要額外橋接或自訂 protocol，適合 Web-only POC，不建議作為跨端最終方案。 |
| 自訂 patch/last-write-wins | 不採用。會在多人同時輸入時覆蓋內容，無法達成 Google Docs 類需求。 |

---

## 3. 文件與身份模型

### 3.1 Document ID

保留舊 lock key 的語意，但在 v3 稱為 `documentId`。

| 文件 | documentId | 說明 |
|---|---|---|
| 班級日誌 | `journal:{className}:{date}` | 例如 `journal:向日葵班:2026-04-26` |
| 學生備註 | `note:{studentId}:{date}` | 例如 `note:B225851150:2026-04-26` |

規則：

1. `documentId` 必須穩定、可重建、不可使用隨機值。
2. `className`、`studentId`、`date` 必須由後端驗證權限。
3. Web、iOS、後端都使用相同字串格式。

### 3.2 Actor / Device / Session

```json
{
  "actorId": "teacher_001",
  "displayName": "王老師",
  "deviceId": "ios_8B2F..." ,
  "sessionId": "sess_01HV...",
  "color": "#2F80ED"
}
```

規則：

1. `actorId` 是教師 userId。
2. `deviceId` 是裝置級身份。Web 存 `localStorage`，iOS 存 Keychain 或 app-scoped secure storage。
3. `sessionId` 每次開啟文件產生新值，用於區分同一裝置的不同視窗或重連。
4. 顏色由 `actorId + deviceId` deterministic hash 產生，避免每次重連變色。
5. 後端不可信任 client body 傳入的 userId；v3 應改由登入 token/session 驗證後產生 actor。

---

## 4. 協作文件資料模型

### 4.1 CRDT Root Schema

每個 `documentId` 對應一份 CRDT document。

班級日誌：

```json
{
  "schemaVersion": 1,
  "kind": "journal",
  "className": "向日葵班",
  "date": "2026-04-26",
  "contentBlocks": [
    {
      "id": "blk_...",
      "type": "plaintext",
      "content": "今天我們..."
    }
  ]
}
```

學生備註：

```json
{
  "schemaVersion": 1,
  "kind": "note",
  "studentId": "B225851150",
  "date": "2026-04-26",
  "note": {
    "health": "",
    "mood": "",
    "appetite": "",
    "nap": "",
    "bowel": "",
    "hideJournal": false,
    "blocks": []
  }
}
```

規則：

1. block 必須永遠有穩定 `id`。
2. 編輯 block 內容時，使用 `block.id` 定位，不用 array index 當永久身份。
3. block 排序可以是 CRDT list；若使用 snapshot materialization，仍輸出成目前 API 的 array。
4. 富文字 `text` block 在 v3 前期可先維持 Web-only；跨端即時協作先以 `plaintext`、活動、需帶、帶回、圖片 metadata 為優先。

### 4.2 後端資料表限制

目前不新增資料表。協作第一版必須直接沿用現有表：

| 文件 | 寫回資料表 | 寫回欄位 |
|---|---|---|
| `journal:{className}:{date}` | `class_journals` | `content_blocks`, `edited_by`, `updated_at` |
| `note:{studentId}:{date}` | `contact_books` | `original_teacher`, `items_to_bring`, `returned_items`, `survey_id`, `edited_by`, `last_modified` |

規則：

1. WebSocket 連線、participants、presence、正在輸入狀態只放記憶體。
2. server restart 後 presence 全部消失是可接受行為。
3. 即時內容先以記憶體 snapshot 廣播，並 debounce 寫回既有表。
4. 不建立 `collab_documents`、`collab_sessions` 或 CRDT history 表。
5. 若未來要完整 CRDT history，必須另外提案，不可在本階段偷加 schema。

---

## 5. Realtime Transport

### 5.1 WebSocket 為主

新增 WebSocket endpoint：

```text
GET /api/collab/ws?documentId={encodedDocumentId}
```

連線規則：

1. 必須使用 `wss://`。
2. 必須帶登入憑證。Web 可用 cookie/session 或短期 token；iOS 使用 bearer token 或既有登入 token。
3. 後端接受連線前必須驗證教師能存取該 class/student/date。
4. 同一 client 斷線後可用相同 `deviceId`、新 `sessionId` 重連。
5. FCM 只負責喚醒或提示資料有更新，不承擔逐字同步。

### 5.2 HTTP Bootstrap / Fallback

```text
GET /api/collab/documents/{documentId}/bootstrap
```

回傳：

```json
{
  "documentId": "journal:向日葵班:2026-04-26",
  "snapshotVersion": 12,
  "snapshot": {},
  "crdt": "base64-encoded-binary",
  "participants": []
}
```

用途：

1. 首次進入文件。
2. WebSocket 重連後補狀態。
3. 不支援 WebSocket 的舊端只讀最新 snapshot。

---

## 6. Event Envelope

所有 realtime 訊息必須包在同一種 envelope。

```json
{
  "protocolVersion": 1,
  "type": "doc.update",
  "documentId": "journal:向日葵班:2026-04-26",
  "sessionId": "sess_01HV...",
  "clientSeq": 41,
  "serverSeq": 108,
  "sentAt": "2026-04-26T21:30:00+08:00",
  "actor": {
    "actorId": "teacher_001",
    "displayName": "王老師",
    "deviceId": "ios_8B2F...",
    "color": "#2F80ED"
  },
  "payload": {}
}
```

Event types：

| type | payload | 是否持久化 | 說明 |
|---|---|---:|---|
| `doc.update` | live snapshot update | 是 | 文件內容變更 |
| `presence.update` | presence state | 否 | 游標、focus、是否正在輸入 |
| `presence.leave` | sessionId | 否 | 離開文件 |
| `snapshot.saved` | updatedAt | 是 | server 已寫回既有資料表 |
| `document.deleted` | deletedBy, deletedAt | 是 | 整份文件刪除 |
| `error` | code, message | 否 | 權限、格式、版本錯誤 |

規則：

1. client 必須遞增 `clientSeq`。
2. server 廣播時必須加上遞增 `serverSeq`。
3. client 收到自己的 echo 可以用 `sessionId + clientSeq` 去重。
4. unknown `type` 必須忽略並記 log，不可讓 editor crash。

---

## 7. Presence 與游標規則

Presence payload：

```json
{
  "focus": {
    "path": "contentBlocks.blk_abc.content",
    "blockId": "blk_abc",
    "field": "content"
  },
  "selection": {
    "anchor": 12,
    "head": 18
  },
  "isTyping": true,
  "preview": "正在輸入的附近文字"
}
```

規則：

1. presence 不寫入資料表。
2. presence 需要節流，建議每 80-150ms 最多送一次。
3. 文字輸入時至少送 `focus.path` 與 `isTyping`；能取得 selection 的平台才送 `selection`。
4. 遠端游標顯示名稱用 `displayName`，顏色用 actor color。
5. iOS 第一階段可以只顯示「王老師正在編輯此 block/此學生」，第二階段再補精準文字游標。
6. 使用中文輸入法 composition 時，不要把未完成組字當成已提交文字覆蓋遠端；只能透過 CRDT transaction 或 editor composition event 送安全更新。

---

## 8. 持久化與舊 API 相容

Server 收到 `doc.update` 後：

1. 套用 update 到記憶體中的 document snapshot。
2. 立即廣播給同一 `documentId` 的其他線上 client。
3. debounce 500-2000ms 後寫回現有資料表：
   - `journal:*` → `class_journals.content_blocks`
   - `note:*` → `contact_books.original_teacher`
4. 寫回成功後廣播 `snapshot.saved`。
5. 必要時仍發 FCM `data_updated` 給未在線或舊版 client。

規則：

1. 現階段真相來源是「記憶體中的 live snapshot + 既有資料表最後保存版本」。
2. 這不是完整 CRDT history；它是先交付 live editing 體感的相容層。
3. 若 WebSocket 不在線，client 可 fallback 到現有 REST auto-save。
4. 第一次 bootstrap 一律從現有 `class_journals` 或 `contact_books` seed。
5. 舊端透過 REST PUT 儲存時，線上協作端可能收到 `data_updated` 後重新 bootstrap。

---

## 9. Web 前端實作規則

Web 重構目標：

1. 新增 `collaborationService`，負責 bootstrap、WebSocket、live update、presence。
2. `ClassJournalEditor.jsx` 不再直接管理 `heldLocksRef` 作為正常編輯入口。
3. `BlockEditor` 的 `blocks` 來源改為 collaboration session state。
4. 每個 block 編輯器 focus 時送 `presence.update`。
5. 編輯文字、增刪 block、移動 block 都必須透過 WebSocket live update 廣播；未來若導入 CRDT，再升級為 CRDT transaction。
6. UI 顯示 participant chips、遠端 block focus badge、可行時顯示文字游標。
7. 舊 `lockService` 僅保留 publish/delete/clear-date 這類 destructive action。

Feature flag：

```text
VITE_COLLAB_EDITING=true
```

若關閉，仍走 v2 鎖定流程，方便逐步上線與回退。

---

## 10. Swift App 接入規則

目前 Swift App 狀態：

1. `WenTzaoConnectApp.swift` 只建立 `ContentView()`，沒有 shared collaboration service。
2. `TeacherDataStore` 以輪詢刷新資料，沒有 WebSocket。
3. `APIClient.swift` 只有 class journal REST GET/PUT/DELETE。
4. `ClassJournalDetailView` 會 3 秒 debounce 自動 PUT，且沒有送 `lastUpdatedAt`，多人同時編輯時可能覆蓋。

v3 Swift 必須新增：

| 元件 | 建議責任 |
|---|---|
| `CollaborationClient` | WebSocket 連線、reconnect、send/receive envelope |
| `CollaborationDocumentStore` | CRDT document 狀態、snapshot materialization、presence map |
| `DeviceIdentityStore` | 產生並保存 `deviceId` |
| `JournalCollaborationSession` | `ClassJournalDetailView` 的文件 session |

SwiftUI 規則：

1. app root 建立可重用的 collaboration service，透過 environment 或明確 initializer 注入畫面。
2. `ClassJournalDetailView.task(id:)` join `journal:{className}:{date}`。
3. `onDisappear` 與 scenePhase 進入 background 時送 `presence.leave`，並關閉或暫停 session。
4. UI state 由 collaboration session 驅動；不要再把 `editableBlocks` 視為只屬於本機的真相。
5. 本機操作先套用到本機 CRDT，再送 `doc.update`，讓輸入手感不被網路阻塞。
6. 收到遠端 update 後更新 CRDT，再用 MainActor 更新 SwiftUI。
7. 第一階段可先顯示 participant chips 與 block-level editing badge；精準文字游標等 TextEditor/自訂 editor 能提供 selection 後再做。

Swift WebSocket：

1. 使用 `URLSessionWebSocketTask` 或專案決定的 WebSocket library。
2. receive loop 必須持續呼叫 receive；斷線要 exponential backoff。
3. app 進 background 時不可假設連線持續存在；回 active 後重新 bootstrap + reconnect。

---

## 11. 後端部署規則

目前 backend 是 Flask + WSGI 風格。長連線協作可以先由 Flask-Sock 承接，但部署方式要避免會 monkey patch 標準 socket/select 的 worker，以免影響 APNs HTTP/2、一般 requests 與未來 Python/Ubuntu 相容性。

第一版改採 Flask + Gunicorn `gthread` + single worker，presence 放記憶體，並直接寫回現有資料表。若未來流量變大，再評估以下路線：

### 目前部署

1. 使用 `gunicorn --worker-class gthread --workers 1 --threads 12 --bind 0.0.0.0:5200 app:app`。
2. 不再使用 `eventlet`，避免 green select/socket 影響 APNs HTTP/2 client。
3. `WEB_WORKERS` 預設維持 `1`，因為 presence 與 connected sessions 仍在單一 process 記憶體中。
4. 若要增加 worker，必須先導入 Redis/pub-sub 或獨立 collaboration service，否則不同 worker 間看不到彼此 presence。

### A. Node collaboration sidecar（優先）

1. 新增 `kindergarten-contactbook-collab` sidecar。
2. 使用 Automerge Repo WebSocket adapter。
3. sidecar 負責 CRDT sync、presence、WebSocket。
4. Flask 保留 REST、權限、materialize API。
5. sidecar 透過內網呼叫 Flask 或共用 SQLite 寫回現有資料表，不新增協作資料表。

### B. Python ASGI collaboration service

1. 新增 ASGI app，使用 WebSocket。
2. 自行處理 live update 與 presence。
3. Flask REST 與 ASGI WS 可在 nginx 分流。

不建議：

1. 用 FCM 傳每次鍵盤輸入。
2. 用 REST polling 模擬逐字同步。
3. 用整份 JSON 覆蓋當作多人協作。

---

## 12. Migration Plan

### Phase 0 — 現況凍結

1. 保留 `docs/editing-lock-system.md` 作為 v2 文件。
2. 不再擴充 `editing_locks` 正常編輯用途。
3. 補測目前 v2 行為，避免重構時不知道舊行為。

### Phase 1 — Collab 基礎建設

1. 建 bootstrap API，從現有資料表讀 snapshot。
2. 建 WebSocket service，presence 放記憶體。
3. 收到 live update 後 debounce 寫回現有資料表。
4. 不新增 schema。

### Phase 2 — Web POC

1. 只開 `journal:{className}:{date}`。
2. 先支援完整 `contentBlocks` snapshot 即時同步、增刪 block、排序。
3. 顯示 participant chips 與正在輸入狀態。
4. feature flag 小範圍測試。

### Phase 3 — 學生備註

1. 將 `note:{studentId}:{date}` 接入協作。
2. 支援健康欄位、備註 blocks、hideJournal。
3. 大量學生卡片只 join 目前 focus/open 的 note document，避免一次開全班所有 WebSocket 文件。

### Phase 4 — Swift App

1. 新增 `CollaborationClient` 與 device identity。
2. `ClassJournalDetailView` 改為 join collaboration session。
3. 先支援班級日誌，再支援學生備註。
4. 加入 scenePhase reconnect、background leave、local cache。

### Phase 5 — 收斂舊鎖

1. `editing_locks` 只保留 destructive action 或移除。
2. v2 FCM lock event 停用。
3. REST save 轉為 CRDT transaction。

---

## 13. 測試清單

必測案例：

1. 兩位不同老師同時編輯同一段文字，雙方文字都保留。
2. 同一老師 Web + iOS 同時編輯，同步顯示兩個裝置 presence。
3. Web 兩個分頁同時開同文件，不互相鎖死。
4. 中文輸入法組字期間不產生亂碼、不重複字。
5. 一人新增 block、另一人編輯既有 block，排序與內容都正確。
6. 一人刪除 block，另一人的游標在該 block 時 UI 能安全移走。
7. 斷線後繼續輸入，重連後合併。
8. app background 後 presence 消失，回 active 後重新出現。
9. server restart 後 client 能 bootstrap 最新 snapshot。
10. 舊家長端仍能透過既有 API 看到 materialized 內容。
11. feature flag 關閉時 v2 鎖定流程仍可使用。

---

## 14. 實作守則摘要

必須：

1. 所有協作端共用 `documentId`、actor、presence、event envelope 定義。
2. 正常編輯使用 WebSocket live update；若未來引入 CRDT，仍不得新增資料表除非先更新本規格。
3. presence 暫存，不進正式內容。
4. 後端 materialize 到舊資料表維持相容。
5. 每個 block 保持穩定 id。
6. Swift 與 Web 都要能處理自己的 echo、遠端 update、斷線重連。

禁止：

1. 用 `editing_locks` 阻擋正常多人編輯。
2. 在未接 WebSocket 的情況下用整份 JSON last-write-wins 假裝協作同步。
3. 信任 client 傳來的 userId 作為權限依據。
4. 把游標、正在輸入、在線狀態寫進正式內容。
5. 未經規格更新就新增協作資料表。

---

## 15. 參考來源

這些來源用於確認跨端協作與 WebSocket 技術方向：

1. Automerge Swift：官方說明其 Swift binding 可用於 browser 與 native app 的協作。
   https://github.com/automerge/automerge-swift
2. Automerge Repo：官方文件描述 storage/network adapter 與 WebSocket server adapter。
   https://automerge.org/docs/reference/repositories/
3. Yjs Awareness：官方文件描述 presence、cursor、awareness update 的語意。
   https://docs.yjs.dev/api/about-awareness
4. Hocuspocus persistence：官方文件提醒 Yjs document 應以 binary update 持久化，不應反覆 JSON 重建。
   https://tiptap.dev/docs/hocuspocus/guides/persistence
5. Apple URLSessionWebSocketTask：官方 Foundation WebSocket API。
   https://developer.apple.com/documentation/foundation/urlsessionwebsockettask

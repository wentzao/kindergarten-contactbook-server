# Editing Lock System — 技術文件

> 版本：v2（2026-04-10）
> 適用範圍：後端 API、教師 Web 前端、教師 Mobile App

> v3 重構草案：若要改成 Google Docs 類多人即時協作，請看 `docs/realtime-collaboration-system.md`。本文件只描述現行 v2 悲觀鎖機制。

---

## 1. 概述

編輯鎖（Editing Lock）是一種**悲觀鎖（pessimistic lock）**機制，用於防止多位老師同時編輯同一份班級日誌或學生備註。當老師 A 開始編輯時，系統會自動取得鎖定，其他老師的 UI 會顯示「某某老師編輯中」並禁止編輯。

### 鎖定類型

| Lock Key 格式 | 說明 | 範例 |
|---|---|---|
| `journal:{className}:{date}` | 班級日誌區塊 | `journal:向日葵班:2026-04-10` |
| `note:{studentId}:{date}` | 學生個人備註 | `note:stu_001:2026-04-10` |

---

## 2. 所有權模型（v2）

### 核心原則

**鎖的所有權由 `lock_owner_id`（裝置級別身份）決定。**

`lock_owner_id` 的生成方式確保「同瀏覽器/同裝置的分頁共享，不同裝置各自獨立」。

| 情境 | 結果 |
|---|---|
| 同一老師、同一瀏覽器/裝置、不同分頁 | 允許（re-acquire，更新鎖定） |
| 同一老師、不同裝置（手機 + 電腦） | **拒絕**（顯示「某某老師編輯中」） |
| 不同老師 | 拒絕（顯示「某某老師編輯中」） |

### `lock_owner_id` 的生成方式

| 平台 | 格式 | 儲存位置 | 跨分頁共享？ |
|---|---|---|---|
| Web | `web_{userId}_{browserId}` | `localStorage` | 是（同瀏覽器所有分頁相同） |
| Mobile | `mobile_{userId}_{deviceId}` | `expo-secure-store` | N/A（手機只有一個視窗） |

### v1 問題回顧

v1 的 `lock_owner_id` 是每個分頁隨機生成的唯一識別碼：
- Web 端存在 `sessionStorage`（每開新分頁就不同）
- Mobile 端存在 `SecureStore`（每次安裝不同，但不含 userId）

這導致**同一位老師在不同分頁被視為不同的人**，無法重新取得自己的鎖。

v2 改為使用 `localStorage`（Web）/ `SecureStore`（Mobile）儲存裝置級別 ID，並結合 `userId`。後端仍以 `lock_owner_id` 欄位判斷所有權，但前端生成的 ID 現在能正確代表「同一老師 + 同一裝置」。

---

## 3. 資料庫結構

```sql
CREATE TABLE editing_locks (
    lock_key     VARCHAR(200) PRIMARY KEY,   -- 鎖定標的（journal:... 或 note:...）
    locked_by    VARCHAR(100) NOT NULL,       -- 擁有者的 userId
    lock_owner_id VARCHAR(150),              -- 裝置級別識別（所有權判斷依據）
    locked_by_name VARCHAR(100),             -- 擁有者顯示名稱
    locked_at    VARCHAR(50) NOT NULL,        -- 取得時間 ISO 8601
    expires_at   VARCHAR(50) NOT NULL         -- 過期時間 ISO 8601
);
```

---

## 4. API 端點

### 4.1 Acquire Lock

```
POST /api/locks/acquire
```

**Request Body:**
```json
{
    "lockKey": "journal:向日葵班:2026-04-10",
    "userId": "teacher_001",
    "userName": "王老師",
    "lockOwnerId": "web_teacher_001_a3f8c2"
}
```

**成功回應（已取得 / 重新取得自己的鎖）：**
```json
{
    "acquired": true,
    "lockOwnerId": "web_teacher_001_a3f8c2",
    "expiresAt": "2026-04-10T14:03:00+08:00"
}
```

**失敗回應（被其他老師或同一老師的其他裝置鎖定）：**
```json
{
    "acquired": false,
    "lockedBy": "teacher_002",
    "lockOwnerId": "web_teacher_002_b7d9e1",
    "lockedByName": "李老師",
    "expiresAt": "2026-04-10T14:03:00+08:00"
}
```

**所有權判斷邏輯：**
```
若該 lock_key 已有鎖定：
  ├── lock_owner_id === 請求者 lockOwnerId → 允許（同裝置 re-acquire）
  └── lock_owner_id !== 請求者 lockOwnerId → 拒絕（不同裝置或不同老師）
若無鎖定 → 建立新鎖定
```

### 4.2 Release Lock

```
POST /api/locks/release
```

```json
{
    "lockKey": "journal:向日葵班:2026-04-10",
    "userId": "teacher_001",
    "lockOwnerId": "web_teacher_001_a3f8c2"
}
```

以 `lock_owner_id` 判斷是否有權釋放（僅持有鎖的裝置能釋放）。

### 4.3 Heartbeat

```
POST /api/locks/heartbeat
```

```json
{
    "lockKey": "journal:向日葵班:2026-04-10",
    "userId": "teacher_001",
    "lockOwnerId": "web_teacher_001_a3f8c2"
}
```

延長 `expires_at`，以 `lock_owner_id` 欄位匹配（僅持有鎖的裝置能延長）。

### 4.4 Batch Release

```
POST /api/locks/release-batch
```

```json
{
    "lockKeys": ["journal:向日葵班:2026-04-10", "note:stu_001:2026-04-10"],
    "userId": "teacher_001",
    "lockOwnerId": "web_teacher_001_a3f8c2"
}
```

用於頁面關閉時一次釋放所有鎖。以 `lock_owner_id` 欄位匹配（僅持有鎖的裝置能釋放）。

### 4.5 Get Status

```
GET /api/locks/status?className=向日葵班&date=2026-04-10
```

回傳指定班級 + 日期的所有鎖定狀態。前端以 `lockOwnerId === myLockOwnerId` 判斷「這是我在這台裝置上的鎖」。若 `lockOwnerId` 不同但 `lockedBy === myUserId`，代表是同一老師的其他裝置持有的鎖。

---

## 5. 前端鎖定生命週期

### 5.1 鎖定取得

```
使用者開始編輯（focus / input）
    → 檢查是否已持有鎖（heldLocksRef）
    → 已持有 → 重設 idle timer
    → 未持有 → POST /api/locks/acquire
        → acquired: true → 加入 heldLocksRef，啟動 idle timer
        → acquired: false → 顯示鎖定 overlay（「某某老師編輯中」）
```

### 5.2 鎖定維持（Heartbeat）

- 每 **150 秒**（2.5 分鐘）對所有持有的鎖發送 heartbeat
- 後端 TTL = **3 分鐘**，heartbeat 會重設到期時間
- 若 heartbeat 失敗，鎖將在 3 分鐘後自動過期

### 5.3 鎖定釋放

鎖定透過以下 **多重機制** 釋放（按優先順序）：

| 機制 | 觸發條件 | 平台 |
|---|---|---|
| **Blur debounce** | 焦點離開編輯區域 150ms 後確認 | Web |
| **Idle timer** | 持有鎖後閒置 8 秒未操作 | Web + Mobile |
| **visibilitychange** | 切換分頁 / 最小化瀏覽器 | Web |
| **beforeunload + sendBeacon** | 關閉/重新整理頁面 | Web |
| **AppState listener** | App 切到背景 | Mobile |
| **useFocusEffect cleanup** | 離開畫面（React Navigation） | Mobile |
| **TTL 自動過期** | 超過 3 分鐘無 heartbeat | 後端 |

### 5.4 Blur 處理（v2 修復）

**v1 問題：** 直接使用 `event.relatedTarget` 判斷焦點去向。但 `relatedTarget` 在以下情況為 `null`：
- 點擊不可聚焦的元素（div、span）
- 瀏覽器失去焦點
- 下拉選單互動
- `contains(null)` 回傳 `false`，導致誤釋放

**v2 修正：** 使用 debounce + `document.activeElement` 雙重確認：

```javascript
// Blur handler（ClassJournalEditor / StudentNoteCards）
const handleBlur = useCallback(() => {
    if (blurTimerRef.current) clearTimeout(blurTimerRef.current);
    blurTimerRef.current = setTimeout(() => {
        const activeEl = document.activeElement;
        // 若焦點仍在容器內，不釋放
        if (containerRef.current?.contains(activeEl)) return;
        void releaseHeldLock(lockKey);
    }, 150); // 等 150ms 讓焦點目標穩定
}, [lockKey, releaseHeldLock]);

// Focus handler：取消任何待處理的 blur 釋放
const handleFocus = useCallback(() => {
    if (blurTimerRef.current) {
        clearTimeout(blurTimerRef.current);
        blurTimerRef.current = null;
    }
}, []);
```

**原理：**
1. Blur 觸發後等 150ms（讓新的 focus 目標完成設定）
2. 若在這 150ms 內同一容器收到 focus → 取消釋放
3. 150ms 後檢查 `document.activeElement` 是否仍在容器內
4. 只在焦點確實離開容器後才釋放鎖

---

## 6. 即時同步（FCM）

鎖定變更時，後端透過 FCM silent notification 通知所有老師/管理員：

```json
{
    "type": "lock_acquired",     // 或 "lock_released"
    "lockKey": "journal:向日葵班:2026-04-10",
    "lockedBy": "王老師",
    "lockOwnerId": "web_teacher_001_a3f8c2"
}
```

前端收到後：
1. 若 `lockOwnerId === 我的 lockOwnerId` → 忽略（同裝置自己的事件）
2. 若是 `lock_acquired` → 更新 UI 顯示鎖定
3. 若是 `lock_released` → 移除鎖定 UI

---

## 7. 時序圖

### 正常編輯流程（不同老師）

```
老師 A（Web）                    後端                     老師 B（Web）
    │                              │                           │
    ├── focus 日誌 ────────────────>│                           │
    │   POST /locks/acquire        │                           │
    │<──── acquired: true ─────────│                           │
    │                              │── FCM: lock_acquired ────>│
    │                              │                           │ 顯示「A 編輯中」
    │                              │                           │
    │   [heartbeat 每 150s] ──────>│                           │
    │                              │                           │
    │── blur 日誌 ─────────────────>│                           │
    │   POST /locks/release        │                           │
    │<──── released: true ─────────│                           │
    │                              │── FCM: lock_released ────>│
    │                              │                           │ 解除鎖定
```

### 同一老師、同瀏覽器（不同分頁）→ 允許

```
老師 A（Tab 1）                  後端                   老師 A（Tab 2）
lockOwnerId = web_A_x8f2                               lockOwnerId = web_A_x8f2 (同 localStorage)
    │                              │                           │
    ├── 取得鎖 ───────────────────>│                           │
    │   lock_owner_id = web_A_x8f2 │                           │
    │<── acquired: true ───────────│                           │
    │                              │                           │
    │                              │<── 取得鎖 ────────────────┤
    │                              │    lock_owner_id = web_A_x8f2 (same!)
    │                              │    → 允許，更新鎖定        │
    │                              │──── acquired: true ──────>│
```

### 同一老師、不同裝置 → 拒絕

```
老師 A（電腦 Web）               後端                   老師 A（手機 App）
lockOwnerId = web_A_x8f2                               lockOwnerId = mobile_A_d9k3
    │                              │                           │
    ├── 取得鎖 ───────────────────>│                           │
    │   lock_owner_id = web_A_x8f2 │                           │
    │<── acquired: true ───────────│                           │
    │                              │                           │
    │                              │<── 取得鎖 ────────────────┤
    │                              │    lock_owner_id = mobile_A_d9k3
    │                              │    ≠ web_A_x8f2 → 拒絕！  │
    │                              │──── acquired: false ─────>│
    │                              │                           │ 顯示「你正在其他裝置編輯中」
```

---

## 8. 設定參數

| 參數 | 值 | 位置 |
|---|---|---|
| `LOCK_TTL_MINUTES` | 3 分鐘 | 後端 `lock_routes.py` |
| `LOCK_IDLE_RELEASE_MS` | 8000ms (8秒) | Web `ClassJournalEditor.jsx` / Mobile `useTeacherEditingLocks.ts` |
| `BLUR_DEBOUNCE_MS` | 150ms | Web `ClassJournalEditor.jsx` / `StudentNoteCards.jsx` |
| Heartbeat interval | 150,000ms (2.5分鐘) | Web / Mobile |
| Status sync interval | 60,000ms (1分鐘) | Mobile |

---

## 9. 關鍵檔案索引

### 後端

| 檔案 | 說明 |
|---|---|
| `routes/lock_routes.py` | 所有鎖定 API 端點 |
| `schema.sql` | DB schema（editing_locks 表） |

### Web 前端

| 檔案 | 說明 |
|---|---|
| `src/pages/class-journal/ClassJournalEditor.jsx` | 主要鎖定協調器（journal + notes） |
| `src/components/StudentNoteCards.jsx` | 學生備註卡片（per-card blur 處理） |
| `src/services/api.js` | lockService API client（acquire/release/heartbeat/releaseBatch/getStatus） |

### Mobile App

| 檔案 | 說明 |
|---|---|
| `hooks/useTeacherEditingLocks.ts` | 完整鎖定 hook（取得/釋放/heartbeat/FCM/AppState） |
| `services/LockService.ts` | API 呼叫封裝 |
| `utils/lockOwnerId.ts` | lockOwnerId 生成（v2: `mobile_{userId}_{deviceId}`） |
| `types/teacher.ts` | 型別定義（LockAcquireResponse 等） |

---

## 10. 已知限制與未來改進

### 已知限制

1. **同一老師的鎖卡在其他裝置**：若老師在手機上編輯後 App 異常關閉（未觸發 AppState background），鎖會卡住直到 TTL 過期（3 分鐘）。在此期間同一老師無法從電腦接手。
2. **SQLite 並行寫入**：`acquire_lock` 的 SELECT → INSERT 之間存在 TOCTOU 風險，但 SQLite 的 file-level locking 使實際衝突極低。
3. **過期鎖清理**：僅在 `/acquire` 和 `/status` 端點觸發 `_clean_expired()`，無背景排程。
4. **清除 localStorage/SecureStore 會產生新的 lockOwnerId**：若使用者清除瀏覽器資料，下次會生成新的 browserId，導致無法釋放舊裝置的鎖（等 TTL 過期即可）。

### 未來改進方向

- [ ] 加入 `BEGIN IMMEDIATE` 消除 acquire 的 race condition
- [ ] 背景定期清理過期鎖
- [ ] Idle release 前顯示倒數警告（「鎖定即將釋放」）
- [ ] WebSocket 替代 FCM 輪詢，降低延遲

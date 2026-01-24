# 聯絡簿 JSON 資料庫設計文件

## 概述

本文件定義聯絡簿系統的 JSON 資料結構設計，採用以學生身分證字號為根目錄的檔案系統架構，易於維護且無需傳統資料庫。

---

## ⚠️ 重要規則：雙版本聯絡簿

> [!CAUTION]
> **所有開發系統必須遵守此規則**

由於政府規定幼兒園不得教授美語，聯絡簿系統必須維護**兩個版本**的親師交流內容：

| 版本 | 用途 | 可見對象 |
|------|------|----------|
| `original` | 完整原始內容 (包含美語課程) | 家長、老師 |
| `redacted` | 刪減版 (過濾敏感詞) | 政府檢查用 |

### 資料結構策略 (Flat Structure with Redaction Overlay)

實際 JSON 檔案採用**扁平化結構**存儲 `original` (原始) 內容。若需產生刪減版，則在該紀錄中增加一個 `redacted` 欄位作為**覆蓋層 (Overlay)**。

- **預設檢視 (Original)**: 直接讀取最外層欄位。
- **政府檢視 (Redacted)**: 若 `redacted` 欄位存在，則使用 `redacted` 內的欄位覆蓋最外層的對應欄位 (例如 `teacher.activities` 或 `teacher.note`)。

---

## 資料夾結構

```
data/
├── students/
│   ├── {身分證字號}/                    # 例如: B225851150
│   │   ├── contacts.json                # 聯絡資料 (LINE User ID, Token)
│   │   ├── leave.json                   # 請假紀錄
│   │   ├── meds.json                    # 用藥紀錄
│   │   ├── contact-book/                # 聯絡簿
│   │   │   ├── 2026/
│   │   │   │   ├── 01.json              # 月份檔案
│   │   │   │   └── ...
│   │   │   └── ...
│   │   └── [future]/
│   │
│   └── ...
├── surveys/                             # 問卷調查定義
└── ...
```

---

## 聯絡簿 JSON 格式

### 月份檔案: `contact-book/{year}/{month}.json`

```json
{
  "studentId": "B225851150",
  "year": 2026,
  "month": 1,
  "records": [
    {
      "date": "2026-01-24",
      "dayOfWeek": "六",
      "status": "signed",
      "readAt": "2026-01-23T17:43:27.392Z",
      "signedAt": "2026-01-23T17:43:39.435Z",
      
      "itemsToBring": {
        "items": ["紅包袋", "水壺"],
        "checkedItems": ["水壺"],
        "checkedAt": "2026-01-24T08:00:00+08:00"
      },
      "returnedItems": ["作品集", "睡袋"],
      "attachedItems": ["收據"],
      
      "teacher": {
        "mood": "🎊",
        "health": "良好",
        "appetite": "全部吃完",
        "nap": "13:00-14:30",
        "activities": ["奧福音樂", "美語課"],
        "note": "今天上美語課很開心！",
        "updatedAt": "2026-01-24T16:30:00+08:00",
        "updatedBy": "Teacher Roy"
      },
      
      "comments": [
        {
          "senderId": "parent",
          "name": "家長",
          "content": "謝謝老師！",
          "createdAt": "2026-01-24T18:00:00+08:00"
        }
      ],
      
      "surveyId": "s2", 

      "redacted": {
        "teacher": {
           "activities": ["奧福音樂"],
           "note": "今天上音樂課很開心！"
        }
      }
    }
  ],
  "metadata": { "lastModified": "2026-01-24T01:43:45.113995" }
}
```

### 狀態流程 (Status)

```
pending_teacher → pending_parent → read → signed
     ↑                 ↑            ↑        ↑
  老師未填寫       老師已填寫     家長已讀  家長已簽名
                  家長未讀
```

### 欄位說明

| 欄位 | 類型 | 說明 |
|------|------|------|
| `status` | string | 狀態 (`pending_teacher`, `pending_parent`, `read`, `signed`) |
| `readAt` | string | 家長首次閱讀時間 |
| `signedAt` | string | 家長簽名時間 |
| `itemsToBring` | object | 攜帶物品與勾選狀態 |
| `itemsToBring.items` | array | 老師指定的物品清單 |
| `itemsToBring.checkedItems` | array | 家長已勾選的物品 |
| `returnedItems` | array | 歸還物品 (如: 睡袋) |
| `attachedItems` | array | 隨附物品 (如: 收據) |
| `teacher` | object | 老師填寫的內容 (心情、健康、課程、聯絡事項) |
| `comments` | array | 親師留言板 (取代舊版 parent 物件) |
| `surveyId` | string | 當日關聯的問卷 ID (選填) |
| `redacted` | object | **[政府版修正]** 若存在，以此內容覆蓋 `teacher` 顯示給檢查員 |

---

## 學生聯絡資料: `contacts.json`

```json
{
  "studentId": "B225851150",
  "guardians": [
    {
      "lineUserId": "Uba57d14ec00dc9357ee54bfb73fcbb27",
      "devices": [
        {
          "pushToken": "ExponentPushToken[xxxxxx]",
          "platform": "ios",
          "lastActiveAt": "2026-01-18T20:00:00+08:00"
        }
      ],
      "notificationEnabled": true,
      "linkedAt": "2026-01-10T08:00:00+08:00"
    }
  ]
}
```

---

## API 端點參考

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/contact-book/{studentId}/{year}/{month}` | 取得月份聯絡簿 (預設回傳 original) |
| GET | `/api/contact-book/{studentId}/{year}/{month}?version=redacted` | 取得月份聯絡簿 (自動套用 redacted 覆蓋) |
| PUT | `/api/contact-book/{studentId}/{date}/teacher` | 老師更新 Original 內容 |
| PUT | `/api/contact-book/{studentId}/{date}/parent` | **(已棄用)** 改用 comments |
| POST | `/api/contact-book/{studentId}/{date}/comments` | 新增留言 |
| PUT | `/api/contact-book/{studentId}/{date}/sign` | 家長簽名 |

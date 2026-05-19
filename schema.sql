-- ==============================================================
-- 文藻幼兒園聯絡簿 — Schema v2 (2026-05-19)
-- 設計原則：
--   1. 「發布/通知」是 (class_name, date) 層級的權限事件
--      → class_notification_grants 是「家長可看」的唯一來源
--   2. contact_books 變稀疏表：只在實際有資料時 INSERT
--   3. class_journals 與通知狀態完全解耦
-- ==============================================================

-- ─────────────────────────────────────────────
-- 身分 / 綁定
-- ─────────────────────────────────────────────
DROP TABLE IF EXISTS students;
CREATE TABLE students (
    student_id VARCHAR(50) PRIMARY KEY,
    guardians TEXT
);

DROP TABLE IF EXISTS teacher_profiles;
CREATE TABLE teacher_profiles (
    user_id VARCHAR(100) PRIMARY KEY,
    cname VARCHAR(100),
    ename VARCHAR(100),
    updated_at VARCHAR(50)
);

DROP TABLE IF EXISTS parent_profiles;
CREATE TABLE parent_profiles (
    user_id VARCHAR(100) PRIMARY KEY,
    display_name VARCHAR(200),
    picture_url TEXT,
    picture_data BLOB,
    picture_mime VARCHAR(50) DEFAULT 'image/jpeg',
    updated_at VARCHAR(50)
);

DROP TABLE IF EXISTS student_bindings;
CREATE TABLE student_bindings (
    user_id    VARCHAR(100) NOT NULL,
    student_id VARCHAR(50)  NOT NULL,
    created_at VARCHAR(50),
    PRIMARY KEY (user_id, student_id)
);
CREATE INDEX idx_student_bindings_student_user ON student_bindings(student_id, user_id);

DROP TABLE IF EXISTS teacher_class_memberships;
CREATE TABLE teacher_class_memberships (
    user_id VARCHAR(100) NOT NULL,
    semester VARCHAR(50) NOT NULL,
    class_name VARCHAR(100) NOT NULL,
    is_admin BOOLEAN DEFAULT 0,
    updated_at VARCHAR(50),
    PRIMARY KEY (user_id, semester, class_name)
);
CREATE INDEX idx_tcm_class_semester ON teacher_class_memberships(class_name, semester);

DROP TABLE IF EXISTS student_class_cache;
CREATE TABLE student_class_cache (
    student_id VARCHAR(50) NOT NULL,
    semester VARCHAR(50) NOT NULL,
    class_name VARCHAR(100) NOT NULL,
    chinese_name VARCHAR(100),
    english_name VARCHAR(100),
    updated_at VARCHAR(50),
    PRIMARY KEY (student_id, semester)
);
CREATE INDEX idx_scc_class_semester ON student_class_cache(class_name, semester);

-- ─────────────────────────────────────────────
-- 聯絡簿核心（新模型）
-- ─────────────────────────────────────────────

-- 班級通知開通 — 家長「可看」權限的唯一真實來源。
-- 一筆 grant = 老師在某天對某班按下「通知家長」的事件。
-- student_ids 永遠儲存當下實際通知的學生 ID list（明確列舉，便於稽核）。
DROP TABLE IF EXISTS class_notification_grants;
CREATE TABLE class_notification_grants (
    class_name VARCHAR(100) NOT NULL,
    date VARCHAR(20) NOT NULL,
    notified_at VARCHAR(50) NOT NULL,
    sent_by VARCHAR(100),
    student_ids TEXT NOT NULL,           -- JSON array, e.g. ["sid1","sid2",...]
    cancelled_at VARCHAR(50),            -- 軟刪除（誤發回收）
    cancelled_by VARCHAR(100),
    PRIMARY KEY (class_name, date)
);
CREATE INDEX idx_cng_date ON class_notification_grants(date);

DROP TABLE IF EXISTS class_notification_grant_students;
CREATE TABLE class_notification_grant_students (
    class_name VARCHAR(100) NOT NULL,
    date VARCHAR(20) NOT NULL,
    student_id VARCHAR(50) NOT NULL,
    created_at VARCHAR(50),
    PRIMARY KEY (class_name, date, student_id)
);
CREATE INDEX idx_cngs_student_date ON class_notification_grant_students(student_id, date);
CREATE INDEX idx_cngs_student_class_date ON class_notification_grant_students(student_id, class_name, date);

-- 班級日誌 — 純內容，不再有 notified_at（通知狀態由 grants 管）。
DROP TABLE IF EXISTS class_journals;
CREATE TABLE class_journals (
    id VARCHAR(50) PRIMARY KEY,
    class_name VARCHAR(100) NOT NULL,
    date VARCHAR(20) NOT NULL,
    semester VARCHAR(20),
    content_blocks TEXT DEFAULT '[]',
    edited_by TEXT,
    created_at VARCHAR(50),
    updated_at VARCHAR(50),
    UNIQUE(class_name, date)
);
CREATE INDEX idx_cj_class_date ON class_journals(class_name, date);

-- 個人聯絡簿 — 稀疏表：只在以下情境才 INSERT。
--   • 教師寫個人備註 / 健康欄位 / 交代事項
--   • 家長已讀 / 簽收 / 留言
-- 「家長可看」與否完全由 class_notification_grants 決定，本表不再有 status / notified_at。
DROP TABLE IF EXISTS contact_books;
CREATE TABLE contact_books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id VARCHAR(50) NOT NULL,
    date VARCHAR(20) NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    read_at VARCHAR(50),
    signed_at VARCHAR(50),
    original_teacher TEXT,           -- {blocks, note, mood, health, appetite, nap, bowel, ...}
    edited_by TEXT,
    original_parent TEXT,            -- {note}
    items_to_bring TEXT,
    returned_items TEXT,
    attached_items TEXT,
    comments TEXT,
    survey_id VARCHAR(50),
    parent_signature_url TEXT,
    last_modified VARCHAR(50),
    UNIQUE(student_id, date)
);
CREATE INDEX idx_cb_student_ym ON contact_books(student_id, year, month);
CREATE INDEX idx_cb_student_date_desc ON contact_books(student_id, date DESC);

-- 排程通知（class 層級）
DROP TABLE IF EXISTS scheduled_class_notifications;
CREATE TABLE scheduled_class_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_name VARCHAR(100) NOT NULL,
    date VARCHAR(20) NOT NULL,
    student_ids TEXT NOT NULL,                 -- JSON array
    send_at VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending / sent / cancelled
    sent_by VARCHAR(100),
    sent_at VARCHAR(50),
    created_at VARCHAR(50) NOT NULL,
    updated_at VARCHAR(50),
    error TEXT
);
CREATE INDEX idx_scn_pending_send_at ON scheduled_class_notifications(status, send_at);
CREATE INDEX idx_scn_class_date ON scheduled_class_notifications(class_name, date, status);

-- 通知事件 log（class 層級，取代舊 publish_events + notification_logs）
DROP TABLE IF EXISTS class_notification_events;
CREATE TABLE class_notification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_name VARCHAR(100) NOT NULL,
    date VARCHAR(20) NOT NULL,
    student_ids TEXT,                              -- JSON array
    mode VARCHAR(30) NOT NULL,                     -- immediate / dismissal / batch
    transition VARCHAR(60) NOT NULL,               -- granted / cancelled / sent / failed
    sent_by VARCHAR(100),
    status VARCHAR(30) NOT NULL,
    delivery_attempted BOOLEAN DEFAULT 0,
    sent_count INTEGER DEFAULT 0,
    error TEXT,
    created_at VARCHAR(50) NOT NULL,
    updated_at VARCHAR(50)
);
CREATE INDEX idx_cne_class_date ON class_notification_events(class_name, date, created_at);
CREATE INDEX idx_cne_status_created ON class_notification_events(status, created_at);

-- ─────────────────────────────────────────────
-- 請假 / 用藥 / 公告 / 問卷
-- ─────────────────────────────────────────────
DROP TABLE IF EXISTS leave_records;
CREATE TABLE leave_records (
    id VARCHAR(50) PRIMARY KEY,
    child_id VARCHAR(50) NOT NULL,
    type VARCHAR(50),
    start_date VARCHAR(20),
    end_date VARCHAR(20),
    reason TEXT,
    signature_url TEXT,
    created_by VARCHAR(100),
    created_at VARCHAR(50)
);
CREATE INDEX idx_leave_child ON leave_records(child_id);
CREATE INDEX idx_leave_dates ON leave_records(start_date, end_date);

DROP TABLE IF EXISTS med_records;
CREATE TABLE med_records (
    id VARCHAR(50) PRIMARY KEY,
    child_id VARCHAR(50) NOT NULL,
    type VARCHAR(50),
    start_date VARCHAR(20),
    end_date VARCHAR(20),
    reason TEXT,
    created_by VARCHAR(100),
    created_at VARCHAR(50),
    medication_details TEXT
);
CREATE INDEX idx_med_child ON med_records(child_id);
CREATE INDEX idx_med_dates ON med_records(start_date, end_date);

DROP TABLE IF EXISTS news;
CREATE TABLE news (
    id VARCHAR(50) PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    tag VARCHAR(50),
    cover_image VARCHAR(500),
    content_blocks TEXT,
    author VARCHAR(100),
    is_pinned BOOLEAN DEFAULT 0,
    publish_at VARCHAR(50),
    created_at VARCHAR(50),
    updated_at VARCHAR(50),
    created_by VARCHAR(100),
    survey_id VARCHAR(50),
    target_classes TEXT,
    status VARCHAR(50),
    first_published_at VARCHAR(50),
    pending_draft TEXT
);

DROP TABLE IF EXISTS surveys;
CREATE TABLE surveys (
    id VARCHAR(50) PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    description TEXT,
    due_date VARCHAR(50),
    target_classes TEXT,
    questions TEXT
);

DROP TABLE IF EXISTS survey_responses;
CREATE TABLE survey_responses (
    survey_id VARCHAR(50) NOT NULL,
    child_id VARCHAR(50) NOT NULL,
    answers TEXT,
    timestamp VARCHAR(50),
    submitted_by VARCHAR(100),
    PRIMARY KEY (survey_id, child_id)
);

-- ─────────────────────────────────────────────
-- 推播 / 訂閱
-- ─────────────────────────────────────────────
DROP TABLE IF EXISTS push_tokens;
CREATE TABLE push_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id VARCHAR(100) NOT NULL,
    push_token VARCHAR(200) NOT NULL,
    device_name VARCHAR(100),
    role VARCHAR(20) DEFAULT 'parent',
    provider VARCHAR(20) DEFAULT 'fcm',
    platform VARCHAR(20),
    environment VARCHAR(20),
    bundle_id VARCHAR(200),
    student_ids TEXT,
    created_at VARCHAR(50),
    updated_at VARCHAR(50),
    UNIQUE(user_id, push_token)
);
CREATE INDEX idx_push_tokens_role_user ON push_tokens(role, user_id);

DROP TABLE IF EXISTS notification_preferences;
CREATE TABLE notification_preferences (
    user_id VARCHAR(100) PRIMARY KEY,
    contact_book_notify BOOLEAN DEFAULT 1,
    announcement_notify BOOLEAN DEFAULT 1,
    updated_at VARCHAR(50)
);

DROP TABLE IF EXISTS push_outbox;
CREATE TABLE push_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type VARCHAR(80) NOT NULL,
    recipient_scope VARCHAR(80) NOT NULL,
    recipient_id VARCHAR(100),
    title TEXT,
    body TEXT,
    payload TEXT NOT NULL,
    pref_column VARCHAR(80),
    idempotency_key VARCHAR(200) NOT NULL UNIQUE,
    source_table VARCHAR(80),
    source_id INTEGER,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at VARCHAR(50) NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at VARCHAR(50) NOT NULL,
    updated_at VARCHAR(50),
    sent_at VARCHAR(50)
);
CREATE INDEX idx_push_outbox_due ON push_outbox(status, next_attempt_at);
CREATE INDEX idx_push_outbox_source ON push_outbox(source_table, source_id);
CREATE INDEX idx_push_outbox_status_updated ON push_outbox(status, updated_at);

-- ─────────────────────────────────────────────
-- 教師端：通知信箱 / 留言已讀
-- ─────────────────────────────────────────────
DROP TABLE IF EXISTS teacher_notifications;
CREATE TABLE teacher_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_user_id VARCHAR(100) NOT NULL,
    type VARCHAR(100) NOT NULL,
    title VARCHAR(200),
    body TEXT,
    student_id VARCHAR(50),
    date VARCHAR(20),
    class_name VARCHAR(100),
    status VARCHAR(50),
    payload TEXT,
    read_at VARCHAR(50),
    created_at VARCHAR(50) NOT NULL
);
CREATE INDEX idx_teacher_notifications_recipient_created
    ON teacher_notifications(recipient_user_id, created_at);
CREATE INDEX idx_teacher_notifications_recipient_read
    ON teacher_notifications(recipient_user_id, read_at);

DROP TABLE IF EXISTS teacher_comment_reads;
CREATE TABLE teacher_comment_reads (
    teacher_id VARCHAR(100) NOT NULL,
    student_id VARCHAR(100) NOT NULL,
    last_read_at VARCHAR(50) NOT NULL,
    PRIMARY KEY (teacher_id, student_id)
);

-- ─────────────────────────────────────────────
-- 編輯鎖（教師 web / iOS 協作）
-- ─────────────────────────────────────────────
DROP TABLE IF EXISTS editing_locks;
CREATE TABLE editing_locks (
    lock_key VARCHAR(200) PRIMARY KEY,
    user_id VARCHAR(100) NOT NULL,
    user_name VARCHAR(100),
    lock_owner_id VARCHAR(100),
    acquired_at VARCHAR(50) NOT NULL,
    last_heartbeat VARCHAR(50) NOT NULL
);

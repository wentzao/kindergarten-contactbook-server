DROP TABLE IF EXISTS students;
CREATE TABLE students (
    student_id VARCHAR(50) PRIMARY KEY,
    guardians TEXT
);

DROP TABLE IF EXISTS contact_books;
CREATE TABLE contact_books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id VARCHAR(50) NOT NULL,
    date VARCHAR(20) NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    day_of_week VARCHAR(10),
    status VARCHAR(50),
    read_at VARCHAR(50),
    signed_at VARCHAR(50),
    items_to_bring TEXT,
    returned_items TEXT,
    attached_items TEXT,
    original_teacher TEXT,
    original_parent TEXT,
    redacted TEXT,
    comments TEXT,
    survey_id VARCHAR(50),
    last_modified VARCHAR(50),
    UNIQUE(student_id, date)
);
CREATE INDEX idx_cb_student_ym ON contact_books(student_id, year, month);

DROP TABLE IF EXISTS leave_records;
CREATE TABLE leave_records (
    id VARCHAR(50) PRIMARY KEY,
    child_id VARCHAR(50) NOT NULL,
    type VARCHAR(50),
    start_date VARCHAR(20),
    end_date VARCHAR(20),
    reason TEXT,
    created_by VARCHAR(100),
    created_at VARCHAR(50)
);
CREATE INDEX idx_leave_child ON leave_records(child_id);

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
    status VARCHAR(50)
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

DROP TABLE IF EXISTS push_tokens;
CREATE TABLE push_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id VARCHAR(100) NOT NULL,
    push_token VARCHAR(200) NOT NULL,
    device_name VARCHAR(100),
    role VARCHAR(20) DEFAULT 'parent',
    created_at VARCHAR(50),
    updated_at VARCHAR(50),
    UNIQUE(user_id, push_token)
);

DROP TABLE IF EXISTS notification_preferences;
CREATE TABLE notification_preferences (
    user_id VARCHAR(100) PRIMARY KEY,
    contact_book_notify BOOLEAN DEFAULT 1,
    announcement_notify BOOLEAN DEFAULT 1,
    updated_at VARCHAR(50)
);

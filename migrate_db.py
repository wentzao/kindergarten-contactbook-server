import os
import json
import sqlite3

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(BASE_DIR, 'kindergarten.db')

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def migrate_students(conn):
    print("Migrating students...")
    students_dir = os.path.join(DATA_DIR, 'students')
    if not os.path.exists(students_dir):
        return
    
    count = 0
    for student_id in os.listdir(students_dir):
        student_path = os.path.join(students_dir, student_id)
        if not os.path.isdir(student_path): continue
        
        contacts_file = os.path.join(student_path, 'contacts.json')
        guardians = "[]"
        if os.path.exists(contacts_file):
            try:
                with open(contacts_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    guardians = json.dumps(data.get('guardians', []), ensure_ascii=False)
            except Exception as e:
                print(f"Error reading {contacts_file}: {e}")
                
        conn.execute('''
            INSERT OR REPLACE INTO students (student_id, guardians) 
            VALUES (?, ?)
        ''', (student_id, guardians))
        count += 1
    
    print(f"Migrated {count} students.")

def migrate_contact_books(conn):
    print("Migrating contact books...")
    students_dir = os.path.join(DATA_DIR, 'students')
    if not os.path.exists(students_dir):
        return
    
    count = 0
    for student_id in os.listdir(students_dir):
        cb_dir = os.path.join(students_dir, student_id, 'contact-book')
        if not os.path.exists(cb_dir): continue
        
        for year in os.listdir(cb_dir):
            year_dir = os.path.join(cb_dir, year)
            if not os.path.isdir(year_dir): continue
            
            for month_file in os.listdir(year_dir):
                if not month_file.endswith('.json'): continue
                
                filepath = os.path.join(year_dir, month_file)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    for record in data.get('records', []):
                        date = record.get('date')
                        if not date: continue
                        
                        original_teacher = record.get('original', {}).get('teacher')
                        if 'teacher' in record and 'original' not in record:
                            # Handling legacy flat structure without original overlay
                            original_teacher = record.get('teacher')
                        
                        original_parent = record.get('original', {}).get('parent')
                        if 'parent' in record and 'original' not in record:
                            original_parent = record.get('parent')
                            
                        conn.execute('''
                            INSERT OR IGNORE INTO contact_books (
                                student_id, date, year, month, day_of_week, status,
                                read_at, signed_at, items_to_bring, returned_items,
                                attached_items, original_teacher, original_parent, redacted,
                                comments, survey_id, last_modified
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            student_id,
                            date,
                            int(year),
                            int(month_file.replace('.json', '')),
                            record.get('dayOfWeek'),
                            record.get('status'),
                            record.get('readAt'),
                            record.get('signedAt'),
                            json.dumps(record.get('itemsToBring'), ensure_ascii=False) if record.get('itemsToBring') else None,
                            json.dumps(record.get('returnedItems', []), ensure_ascii=False),
                            json.dumps(record.get('attachedItems', []), ensure_ascii=False),
                            json.dumps(original_teacher, ensure_ascii=False) if original_teacher else None,
                            json.dumps(original_parent, ensure_ascii=False) if original_parent else None,
                            json.dumps(record.get('redacted'), ensure_ascii=False) if record.get('redacted') else None,
                            json.dumps(record.get('comments', []), ensure_ascii=False),
                            record.get('surveyId'),
                            data.get('metadata', {}).get('lastModified')
                        ))
                        count += 1
                except Exception as e:
                    print(f"Error migrating {filepath}: {e}")
                    import traceback
                    traceback.print_exc()

    print(f"Migrated {count} contact book records.")

def migrate_leave_and_meds(conn):
    print("Migrating leave and med records from centralized store...")
    
    # Process centralized leave vs med
    for data_type in ['leave', 'meds']:
        folder = os.path.join(DATA_DIR, data_type)
        if not os.path.exists(folder): continue
        count = 0
        for file in os.listdir(folder):
            if not file.endswith('.json'): continue
            path = os.path.join(folder, file)
            with open(path, 'r', encoding='utf-8') as f:
                try:
                    records = json.load(f)
                    for r in records:
                        idx = r.get('id')
                        if not idx: continue
                        
                        if data_type == 'leave':
                            conn.execute('''
                                INSERT OR IGNORE INTO leave_records (
                                    id, child_id, type, start_date, end_date,
                                    reason, created_by, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                idx, r.get('childId'), r.get('type'), r.get('startDate'),
                                r.get('endDate'), r.get('reason'), r.get('createdBy'),
                                r.get('createdAt')
                            ))
                        else:
                            med_details = {k: v for k, v in r.items() if k not in 
                                           ['id', 'childId', 'type', 'startDate', 'endDate', 'reason', 'createdBy', 'createdAt']}
                            conn.execute('''
                                INSERT OR IGNORE INTO med_records (
                                    id, child_id, type, start_date, end_date,
                                    reason, created_by, created_at, medication_details
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                idx, r.get('childId'), r.get('type'), r.get('startDate'),
                                r.get('endDate'), r.get('reason'), r.get('createdBy'),
                                r.get('createdAt'), json.dumps(med_details, ensure_ascii=False)
                            ))
                        count += 1
                except BaseException as e:
                    print(f"Error in {path}: {e}")
        print(f"Migrated {count} {data_type} records.")

def migrate_news(conn):
    print("Migrating news...")
    folder = os.path.join(DATA_DIR, 'news')
    if not os.path.exists(folder): return
    count = 0
    for file in os.listdir(folder):
        if not file.endswith('.json'): continue
        path = os.path.join(folder, file)
        with open(path, 'r', encoding='utf-8') as f:
            try:
                r = json.load(f)
                conn.execute('''
                    INSERT OR IGNORE INTO news (
                        id, title, tag, cover_image, content_blocks, author,
                        is_pinned, publish_at, created_at, updated_at, created_by,
                        survey_id, target_classes, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    r.get('id'), r.get('title'), r.get('tag'), r.get('coverImage'),
                    json.dumps(r.get('contentBlocks', []), ensure_ascii=False),
                    r.get('author'), r.get('isPinned', False), r.get('publishAt'),
                    r.get('createdAt'), r.get('updatedAt'), r.get('createdBy'),
                    r.get('surveyId'), json.dumps(r.get('targetClasses', []), ensure_ascii=False),
                    r.get('status', 'published')
                ))
                count += 1
            except Exception as e:
                print(f"Error {path}: {e}")
    print(f"Migrated {count} news items.")

def migrate_surveys(conn):
    print("Migrating surveys...")
    def_folder = os.path.join(DATA_DIR, 'surveys', 'definitions')
    if os.path.exists(def_folder):
        count = 0
        for file in os.listdir(def_folder):
            if not file.endswith('.json'): continue
            path = os.path.join(def_folder, file)
            with open(path, 'r', encoding='utf-8') as f:
                r = json.load(f)
                conn.execute('''
                    INSERT OR IGNORE INTO surveys (
                        id, title, description, due_date, target_classes, questions
                    ) VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    r.get('id'), r.get('title'), r.get('description'), r.get('dueDate'),
                    json.dumps(r.get('targetClasses', []), ensure_ascii=False),
                    json.dumps(r.get('questions', []), ensure_ascii=False)
                ))
                count += 1
        print(f"Migrated {count} surveys definitions.")
    
    resp_folder = os.path.join(DATA_DIR, 'surveys', 'responses')
    if os.path.exists(resp_folder):
        count = 0
        for file in os.listdir(resp_folder):
            if not file.endswith('.json'): continue
            survey_id = file.replace('.json', '')
            path = os.path.join(resp_folder, file)
            with open(path, 'r', encoding='utf-8') as f:
                responses = json.load(f)
                for child_id, ans in responses.items():
                    conn.execute('''
                        INSERT OR IGNORE INTO survey_responses (
                            survey_id, child_id, answers, timestamp, submitted_by
                        ) VALUES (?, ?, ?, ?, ?)
                    ''', (
                        survey_id, child_id,
                        json.dumps(ans.get('answers', {}), ensure_ascii=False),
                        ans.get('timestamp'), ans.get('submittedBy')
                    ))
                    count += 1
        print(f"Migrated {count} survey responses.")

def main():
    if not os.path.exists(DB_PATH):
        print("Database not initialized. Doing it now...")
        import init_db
        init_db.init_db()
        
    conn = get_db_connection()
    migrate_students(conn)
    migrate_contact_books(conn)
    migrate_leave_and_meds(conn)
    migrate_news(conn)
    migrate_surveys(conn)
    conn.commit()
    conn.close()
    print("Migration completed successfully.")

if __name__ == '__main__':
    main()

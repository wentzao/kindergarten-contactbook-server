import os
import json
import sqlite3
from datetime import datetime

class DataService:
    # Type label translations (English to Chinese)
    LEAVE_TYPES = {
        'personal': '事假',
        'sick': '病假',
        'funeral': '喪假',
        'other': '其他'
    }
    
    MED_REASONS = {
        'cold': '感冒',
        'gastro': '腸胃炎',
        'allergy': '過敏',
        'other': '其他'
    }
    
    def __init__(self, data_dir):
        # We don't use data_dir much anymore, but keep it for config
        default_db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'kindergarten.db')
        self.db_path = (
            os.environ.get('KINDERGARTEN_DB_PATH')
            or os.environ.get('DB_PATH')
            or default_db_path
        )
        self._ensure_schema()

    def get_db(self):
        # timeout=30: Python-level retry for up to 30 seconds on lock
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        """Defensive schema migration — runs every startup, idempotent.

        We use this instead of a versioned migration system because the deltas
        are small and we want zero manual steps on deploy. Each ALTER is
        wrapped in try/except so re-runs are safe.
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            # signature_url for leave_records (added when 家長簽名 feature shipped)
            try:
                conn.execute('ALTER TABLE leave_records ADD COLUMN signature_url TEXT')
                conn.commit()
                print('[DATA_SERVICE] Added signature_url column to leave_records')
            except sqlite3.OperationalError:
                # Column already exists — expected on subsequent startups.
                pass

            # parent_signature_url for contact_books (聯絡簿每日簽收的家長簽名)
            try:
                conn.execute('ALTER TABLE contact_books ADD COLUMN parent_signature_url TEXT')
                conn.commit()
                print('[DATA_SERVICE] Added parent_signature_url column to contact_books')
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()

    def _translate_type(self, data_type, type_value):
        """Translate type value from English to Chinese"""
        if data_type == 'leave':
            return self.LEAVE_TYPES.get(type_value, type_value)
        elif data_type == 'meds':
            return self.MED_REASONS.get(type_value, type_value)
        return type_value

    def save_student_record(self, child_id, data_type, record):
        """Save leave or med record to DB"""
        if 'id' not in record:
            record['id'] = f"{int(datetime.now().timestamp() * 1000)}"
        
        if 'createdAt' not in record:
            record['createdAt'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if 'type' in record:
            record['type'] = self._translate_type(data_type, record['type'])
        
        if 'status' in record:
            del record['status']

        conn = self.get_db()
        try:
            if data_type == 'leave':
                conn.execute('''
                    INSERT INTO leave_records (id, child_id, type, start_date, end_date, reason, signature_url, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    record.get('id'), child_id, record.get('type'), record.get('startDate'),
                    record.get('endDate'), record.get('reason'), record.get('signatureUrl'),
                    record.get('createdBy'), record.get('createdAt')
                ))
            elif data_type == 'meds':
                med_details = {k: v for k, v in record.items() if k not in 
                               ['id', 'childId', 'type', 'startDate', 'endDate', 'reason', 'createdBy', 'createdAt']}
                conn.execute('''
                    INSERT INTO med_records (id, child_id, type, start_date, end_date, reason, created_by, created_at, medication_details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    record.get('id'), child_id, record.get('type'), record.get('startDate'),
                    record.get('endDate'), record.get('reason'), record.get('createdBy'),
                    record.get('createdAt'), json.dumps(med_details, ensure_ascii=False)
                ))
            conn.commit()
            print(f"[DATA_SERVICE] Saved {data_type} record {record['id']} to DB")
        finally:
            conn.close()
            
        return record

    def get_student_records(self, child_id, data_type, timeframe=None):
        """Get records from DB"""
        conn = self.get_db()
        try:
            if data_type == 'leave':
                rows = conn.execute('SELECT * FROM leave_records WHERE child_id = ? ORDER BY created_at DESC', (child_id,)).fetchall()
                records = []
                for r in rows:
                    rec = {
                        'id': r['id'], 'childId': r['child_id'], 'type': r['type'],
                        'startDate': r['start_date'], 'endDate': r['end_date'],
                        'reason': r['reason'], 'createdBy': r['created_by'], 'createdAt': r['created_at']
                    }
                    # signature_url 在舊資料上可能不存在 — 用 try/except 保護以免 row 沒這個 key
                    try:
                        if r['signature_url']:
                            rec['signatureUrl'] = r['signature_url']
                    except (IndexError, KeyError):
                        pass
                    records.append(rec)
                return records
            elif data_type == 'meds':
                rows = conn.execute('SELECT * FROM med_records WHERE child_id = ? ORDER BY created_at DESC', (child_id,)).fetchall()
                records = []
                for r in rows:
                    rec = {
                        'id': r['id'], 'childId': r['child_id'], 'type': r['type'],
                        'startDate': r['start_date'], 'endDate': r['end_date'],
                        'reason': r['reason'], 'createdBy': r['created_by'], 'createdAt': r['created_at']
                    }
                    if r['medication_details']:
                        try:
                            details = json.loads(r['medication_details'])
                            rec.update(details)
                        except:
                            pass
                    records.append(rec)
                return records
        finally:
            conn.close()
        return []

    def delete_student_record(self, record_id, data_type):
        """Delete a record by ID from DB"""
        deleted = False
        conn = self.get_db()
        try:
            table = 'leave_records' if data_type == 'leave' else 'med_records'
            cursor = conn.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
            if cursor.rowcount > 0:
                deleted = True
                print(f"[DATA_SERVICE] Deleted {data_type} record {record_id} from DB")
            conn.commit()
        finally:
            conn.close()
            
        return deleted

    def get_available_surveys(self, child_class=None):
        """List all surveys, optionally filtered by class."""
        surveys = []
        conn = self.get_db()
        try:
            rows = conn.execute('SELECT * FROM surveys').fetchall()
            for r in rows:
                target_classes = json.loads(r['target_classes']) if r['target_classes'] else []
                if child_class and target_classes and child_class not in target_classes:
                    continue
                
                due_date = r['due_date']
                if due_date:
                    from datetime import datetime as dt
                    try:
                        if dt.strptime(due_date, '%Y-%m-%d').date() < dt.now().date():
                            continue
                    except:
                        pass
                
                surveys.append({
                    'id': r['id'],
                    'title': r['title'],
                    'description': r['description'],
                    'dueDate': r['due_date'],
                    'targetClasses': target_classes,
                    'questions': json.loads(r['questions']) if r['questions'] else []
                })
        finally:
            conn.close()
        return surveys

    def get_survey_definition(self, survey_id):
        conn = self.get_db()
        try:
            r = conn.execute('SELECT * FROM surveys WHERE id = ?', (survey_id,)).fetchone()
            if r:
                return {
                    'id': r['id'],
                    'title': r['title'],
                    'description': r['description'],
                    'dueDate': r['due_date'],
                    'targetClasses': json.loads(r['target_classes']) if r['target_classes'] else [],
                    'questions': json.loads(r['questions']) if r['questions'] else []
                }
        finally:
            conn.close()
        return None

    def get_child_survey_status(self, survey_id, child_id):
        conn = self.get_db()
        try:
            r = conn.execute('SELECT 1 FROM survey_responses WHERE survey_id = ? AND child_id = ?', (survey_id, child_id)).fetchone()
            if r:
                return 'completed'
        finally:
            conn.close()
        return 'pending'

    def get_survey_response(self, survey_id, child_id):
        conn = self.get_db()
        try:
            r = conn.execute('SELECT * FROM survey_responses WHERE survey_id = ? AND child_id = ?', (survey_id, child_id)).fetchone()
            if r:
                return {
                    'answers': json.loads(r['answers']) if r['answers'] else {},
                    'timestamp': r['timestamp'],
                    'submittedBy': r['submitted_by']
                }
        finally:
            conn.close()
        return None

    def save_survey_response(self, survey_id, child_id, answers, user_id=''):
        conn = self.get_db()
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ans_json = json.dumps(answers, ensure_ascii=False)

            conn.execute('''
                INSERT INTO survey_responses (survey_id, child_id, answers, timestamp, submitted_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(survey_id, child_id) DO UPDATE SET
                    answers = excluded.answers,
                    timestamp = excluded.timestamp,
                    submitted_by = excluded.submitted_by
            ''', (survey_id, child_id, ans_json, timestamp, user_id))
            conn.commit()
            return True
        finally:
            conn.close()

    # ── Class Journal ──

    @staticmethod
    def _semester_from_date(date_str):
        """Compute semester string (e.g. '114第2學期') from a YYYY-MM-DD date."""
        year, month = int(date_str[:4]), int(date_str[5:7])
        if month >= 8:
            return f"{year - 1911}第1學期"
        elif month == 1:
            return f"{year - 1912}第1學期"
        else:
            return f"{year - 1912}第2學期"

    def get_class_journal(self, class_name, date):
        conn = self.get_db()
        try:
            r = conn.execute(
                'SELECT * FROM class_journals WHERE class_name = ? AND date = ?',
                (class_name, date)
            ).fetchone()
            if r:
                return {
                    'id': r['id'],
                    'className': r['class_name'],
                    'date': r['date'],
                    'semester': r['semester'],
                    'contentBlocks': json.loads(r['content_blocks']) if r['content_blocks'] else [],
                    'editedBy': json.loads(r['edited_by']) if r['edited_by'] else None,
                    'notifiedAt': r['notified_at'],
                    'createdAt': r['created_at'],
                    'updatedAt': r['updated_at'],
                }
            return None
        finally:
            conn.close()

    def save_class_journal(self, class_name, date, content_blocks, edited_by):
        conn = self.get_db()
        try:
            now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
            journal_id = f"cj_{class_name}_{date}"
            semester = self._semester_from_date(date)
            blocks_json = json.dumps(content_blocks, ensure_ascii=False)
            edited_json = json.dumps(edited_by, ensure_ascii=False) if edited_by else None

            conn.execute('''
                INSERT INTO class_journals (id, class_name, date, semester, content_blocks, edited_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(class_name, date) DO UPDATE SET
                    content_blocks = excluded.content_blocks,
                    edited_by = excluded.edited_by,
                    updated_at = excluded.updated_at
            ''', (journal_id, class_name, date, semester, blocks_json, edited_json, now, now))
            conn.commit()
            return {
                'id': journal_id,
                'className': class_name,
                'date': date,
                'semester': semester,
                'contentBlocks': content_blocks,
                'editedBy': edited_by,
                'updatedAt': now,
            }
        finally:
            conn.close()

    def publish_class_journal(self, class_name, date):
        """Mark class journal as notified/published."""
        conn = self.get_db()
        try:
            now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
            cursor = conn.execute(
                'UPDATE class_journals SET notified_at = ? WHERE class_name = ? AND date = ?',
                (now, class_name, date)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_class_journal(self, class_name, date):
        conn = self.get_db()
        try:
            cursor = conn.execute(
                'DELETE FROM class_journals WHERE class_name = ? AND date = ?',
                (class_name, date)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

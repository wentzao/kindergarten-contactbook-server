import os
import json
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
        self.data_dir = data_dir

    def _ensure_dir(self, path):
        os.makedirs(path, exist_ok=True)

    def _get_centralized_file_path(self, data_type, timeframe=None):
        """Get path for centralized data file: data/leave/YYYYMM.json or data/meds/YYYYMM.json"""
        if not timeframe:
            timeframe = datetime.now().strftime('%Y%m')
            
        base_dir = os.path.join(self.data_dir, data_type)
        self._ensure_dir(base_dir)
        return os.path.join(base_dir, f"{timeframe}.json")

    def _get_student_file_path(self, child_id, data_type):
        """Get path for individual student file: data/students/{child_id}/leave.json (single file)"""
        base_dir = os.path.join(self.data_dir, 'students', child_id)
        self._ensure_dir(base_dir)
        return os.path.join(base_dir, f"{data_type}.json")

    def _get_survey_def_path(self, survey_id=None):
        # Path: data/surveys/definitions/{survey_id}.json
        base_dir = os.path.join(self.data_dir, 'surveys', 'definitions')
        self._ensure_dir(base_dir)
        if survey_id:
            return os.path.join(base_dir, f"{survey_id}.json")
        return base_dir

    def _get_survey_response_path(self, survey_id):
        # Path: data/surveys/responses/{survey_id}.json
        base_dir = os.path.join(self.data_dir, 'surveys', 'responses')
        self._ensure_dir(base_dir)
        return os.path.join(base_dir, f"{survey_id}.json")

    def _translate_type(self, data_type, type_value):
        """Translate type value from English to Chinese"""
        if data_type == 'leave':
            return self.LEAVE_TYPES.get(type_value, type_value)
        elif data_type == 'meds':
            return self.MED_REASONS.get(type_value, type_value)
        return type_value

    def save_student_record(self, child_id, data_type, record):
        """Save record to both centralized and individual student files"""
        # Add metadata
        if 'id' not in record:
            record['id'] = f"{int(datetime.now().timestamp() * 1000)}"
        
        if 'createdAt' not in record:
            record['createdAt'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Translate type to Chinese and replace the original type field
        if 'type' in record:
            record['type'] = self._translate_type(data_type, record['type'])
        
        # Remove status field if present (no approval workflow needed)
        if 'status' in record:
            del record['status']
        
        # 1. Save to centralized file (e.g., data/leave/202601.json)
        centralized_path = self._get_centralized_file_path(data_type)
        self._save_to_file(centralized_path, record)
        print(f"[DATA_SERVICE] Saved to centralized: {centralized_path}")
        
        # 2. Save to individual student file (e.g., data/students/{id}/leave.json - single file)
        student_path = self._get_student_file_path(child_id, data_type)
        self._save_to_file(student_path, record)
        print(f"[DATA_SERVICE] Saved to student folder: {student_path}")
        
        return record

    def _save_to_file(self, file_path, record):
        """Helper to append record to a JSON file"""
        data = []
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = []

        data.append(record)

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_student_records(self, child_id, data_type, timeframe=None):
        """Get records from individual student folder (single file, ignore timeframe)"""
        file_path = self._get_student_file_path(child_id, data_type)
        if os.path.exists(file_path):
             with open(file_path, 'r', encoding='utf-8') as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    return []
        return []

    def delete_student_record(self, record_id, data_type):
        """Delete a record by ID from all files"""
        deleted = False
        
        # 1. Delete from centralized file
        centralized_path = self._get_centralized_file_path(data_type)
        if os.path.exists(centralized_path):
            with open(centralized_path, 'r', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = []
            
            original_len = len(data)
            data = [r for r in data if r.get('id') != record_id]
            
            if len(data) < original_len:
                with open(centralized_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                deleted = True
                print(f"[DATA_SERVICE] Deleted from centralized: {centralized_path}")
        
        # 2. Find and delete from individual student files
        students_dir = os.path.join(self.data_dir, 'students')
        if os.path.exists(students_dir):
            for student_id in os.listdir(students_dir):
                student_file = os.path.join(students_dir, student_id, f'{data_type}.json')
                if os.path.exists(student_file):
                    with open(student_file, 'r', encoding='utf-8') as f:
                        try:
                            data = json.load(f)
                        except json.JSONDecodeError:
                            continue
                    
                    original_len = len(data)
                    data = [r for r in data if r.get('id') != record_id]
                    
                    if len(data) < original_len:
                        with open(student_file, 'w', encoding='utf-8') as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        deleted = True
                        print(f"[DATA_SERVICE] Deleted from student file: {student_file}")
        
        return deleted

    def get_available_surveys(self, child_class=None):
        """
        List all surveys, optionally filtered by class.
        Args:
            child_class: If provided, filter surveys where targetClasses includes this class
                         or targetClasses is empty/missing (applies to all)
        """
        base_dir = self._get_survey_def_path()
        surveys = []
        if os.path.exists(base_dir):
            for filename in os.listdir(base_dir):
                if filename.endswith('.json'):
                    with open(os.path.join(base_dir, filename), 'r', encoding='utf-8') as f:
                        try:
                            survey = json.load(f)
                            # Check class targeting
                            target_classes = survey.get('targetClasses', [])
                            if child_class and target_classes:
                                # If target classes specified, check if child's class matches
                                if child_class not in target_classes:
                                    continue
                            # Check if expired (optional - skip expired surveys)
                            due_date = survey.get('dueDate')
                            if due_date:
                                from datetime import datetime as dt
                                try:
                                    if dt.strptime(due_date, '%Y-%m-%d').date() < dt.now().date():
                                        continue  # Skip expired
                                except:
                                    pass
                            surveys.append(survey)
                        except:
                            pass
        return surveys

    def get_survey_definition(self, survey_id):
        """
        Get a single survey definition by ID.
        Returns the survey dict or None if not found.
        """
        file_path = self._get_survey_def_path(survey_id)
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                try:
                    return json.load(f)
                except:
                    return None
        return None


    def get_child_survey_status(self, survey_id, child_id):
        """
        Check if a child has completed a survey.
        Returns: 'completed' or 'pending'
        """
        file_path = self._get_survey_response_path(survey_id)
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                try:
                    responses = json.load(f)
                    if child_id in responses:
                        return 'completed'
                except:
                    pass
        return 'pending'

    def get_survey_response(self, survey_id, child_id):
        """
        Get existing survey response for a child.
        Returns the response dict (answers, timestamp, submittedBy) or None if not found.
        """
        file_path = self._get_survey_response_path(survey_id)
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                try:
                    responses = json.load(f)
                    if child_id in responses:
                        return responses[child_id]
                except:
                    pass
        return None

    def save_survey_response(self, survey_id, child_id, answers, user_id=''):
        file_path = self._get_survey_response_path(survey_id)
        
        # Ensure responses directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        responses = {}
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                try:
                    responses = json.load(f)
                except:
                    responses = {}
        
        # Structure: { "child_id": { answers, timestamp, submittedBy } }
        responses[child_id] = {
            "answers": answers,
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "submittedBy": user_id
        }

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(responses, f, ensure_ascii=False, indent=2)

        return True


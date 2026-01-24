from flask import Blueprint, request, jsonify
import os
import json

contact_book_bp = Blueprint('contact_book', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')

def get_student_contact_book_path(student_id, year, month):
    """Get path to contact book JSON file"""
    return os.path.join(DATA_DIR, 'students', student_id, 'contact-book', str(year), f'{month:02d}.json')

@contact_book_bp.route('/<student_id>/<int:year>/<int:month>', methods=['GET'])
def get_contact_book(student_id, year, month):
    """
    Get contact book for a student for a specific month.
    Query param: version=original (default) or version=redacted
    """
    version = request.args.get('version', 'original')
    print(f"[CONTACT_BOOK GET] student_id={student_id}, year={year}, month={month}, version={version}")
    
    filepath = get_student_contact_book_path(student_id, year, month)
    
    if not os.path.exists(filepath):
        print(f"[CONTACT_BOOK GET] File not found: {filepath}")
        return jsonify({'error': 'Contact book not found'}), 404
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # If requesting redacted version, filter the records
        if version == 'redacted':
            for record in data.get('records', []):
                if record.get('redacted'):
                    record['teacher'] = record['redacted'].get('teacher')
                    record['parent'] = record['redacted'].get('parent')
                elif record.get('original'):
                    record['teacher'] = record['original'].get('teacher')
                    record['parent'] = record['original'].get('parent')
        else:
            # Return original version
            for record in data.get('records', []):
                if record.get('original'):
                    record['teacher'] = record['original'].get('teacher')
                    record['parent'] = record['original'].get('parent')
        
        print(f"[CONTACT_BOOK GET] Returning {len(data.get('records', []))} records")
        return jsonify(data)
    except Exception as e:
        print(f"[CONTACT_BOOK GET] Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@contact_book_bp.route('/<student_id>/<date>/parent', methods=['PUT'])
def update_parent_entry(student_id, date):
    """Update parent's entry for a specific date"""
    try:
        print(f"[CONTACT_BOOK PUT PARENT] student_id={student_id}, date={date}")
        data = request.json
        
        # Parse date to get year and month
        year, month, day = date.split('-')
        filepath = get_student_contact_book_path(student_id, int(year), int(month))
        
        # Load existing data
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                contact_book = json.load(f)
        else:
            # Create new file structure
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            contact_book = {
                'studentId': student_id,
                'year': int(year),
                'month': int(month),
                'records': [],
                'metadata': {}
            }
        
        # Find or create record for the date
        record = next((r for r in contact_book['records'] if r['date'] == date), None)
        if not record:
            record = {
                'date': date,
                'dayOfWeek': '',
                'status': 'pending_teacher',
                'original': {'teacher': None, 'parent': None},
                'redacted': None
            }
            contact_book['records'].append(record)
        
        # Update parent entry in original
        if 'original' not in record:
            record['original'] = {'teacher': None, 'parent': None}
        record['original']['parent'] = data
        
        # Update metadata
        from datetime import datetime
        contact_book['metadata']['lastModified'] = datetime.now().isoformat()
        
        # Save
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(contact_book, f, ensure_ascii=False, indent=2)
        
        print(f"[CONTACT_BOOK PUT PARENT] Successfully updated")
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        print(f"[CONTACT_BOOK PUT PARENT] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@contact_book_bp.route('/<student_id>/<date>/teacher', methods=['PUT'])
def update_teacher_entry(student_id, date):
    """Update teacher's entry for a specific date"""
    try:
        print(f"[CONTACT_BOOK PUT TEACHER] student_id={student_id}, date={date}")
        data = request.json
        
        # Parse date to get year and month
        year, month, day = date.split('-')
        filepath = get_student_contact_book_path(student_id, int(year), int(month))
        
        # Load existing data
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                contact_book = json.load(f)
        else:
            return jsonify({'error': 'Contact book not found'}), 404
        
        # Find record for the date
        record = next((r for r in contact_book['records'] if r['date'] == date), None)
        if not record:
            return jsonify({'error': 'Record for date not found'}), 404
        
        # Update teacher entry in original
        if 'original' not in record:
            record['original'] = {'teacher': None, 'parent': None}
        record['original']['teacher'] = data
        record['status'] = 'completed'
        
        # Update metadata
        from datetime import datetime
        contact_book['metadata']['lastModified'] = datetime.now().isoformat()
        
        # Save
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(contact_book, f, ensure_ascii=False, indent=2)
        
        print(f"[CONTACT_BOOK PUT TEACHER] Successfully updated")
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        print(f"[CONTACT_BOOK PUT TEACHER] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@contact_book_bp.route('/<student_id>/latest', methods=['GET'])
def get_latest_records(student_id):
    """Get the latest N contact book records for a student"""
    limit = request.args.get('limit', 10, type=int)
    print(f"[CONTACT_BOOK LATEST] student_id={student_id}, limit={limit}")
    
    student_dir = os.path.join(DATA_DIR, 'students', student_id, 'contact-book')
    
    if not os.path.exists(student_dir):
        return jsonify({'error': 'Student not found'}), 404
    
    all_records = []
    
    # Iterate through years and months
    for year_dir in sorted(os.listdir(student_dir), reverse=True):
        year_path = os.path.join(student_dir, year_dir)
        if not os.path.isdir(year_path):
            continue
        for month_file in sorted(os.listdir(year_path), reverse=True):
            if not month_file.endswith('.json'):
                continue
            filepath = os.path.join(year_path, month_file)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for record in data.get('records', []):
                    # Flatten original to top level for convenience
                    if record.get('original'):
                        record['teacher'] = record['original'].get('teacher')
                        record['parent'] = record['original'].get('parent')
                    all_records.append(record)
            except:
                continue
    
    # Sort by date descending
    all_records.sort(key=lambda x: x.get('date', ''), reverse=True)
    
    print(f"[CONTACT_BOOK LATEST] Returning {min(limit, len(all_records))} records")
    return jsonify(all_records[:limit])

@contact_book_bp.route('/<student_id>/<date>/read', methods=['PUT'])
def mark_as_read(student_id, date):
    """Mark a contact book entry as read by parent"""
    try:
        print(f"[CONTACT_BOOK READ] student_id={student_id}, date={date}")
        data = request.json or {}
        
        year, month, day = date.split('-')
        filepath = get_student_contact_book_path(student_id, int(year), int(month))
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'Contact book not found'}), 404
        
        with open(filepath, 'r', encoding='utf-8') as f:
            contact_book = json.load(f)
        
        record = next((r for r in contact_book['records'] if r['date'] == date), None)
        if not record:
            return jsonify({'error': 'Record not found'}), 404
        
        # Only update if currently pending_parent
        if record.get('status') == 'pending_parent':
            record['status'] = 'read'
            record['readAt'] = data.get('readAt') or __import__('datetime').datetime.now().isoformat()
            
            contact_book['metadata']['lastModified'] = __import__('datetime').datetime.now().isoformat()
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(contact_book, f, ensure_ascii=False, indent=2)
        
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        print(f"[CONTACT_BOOK READ] Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@contact_book_bp.route('/<student_id>/<date>/sign', methods=['PUT'])
def mark_as_signed(student_id, date):
    """Mark a contact book entry as signed by parent"""
    try:
        print(f"[CONTACT_BOOK SIGN] student_id={student_id}, date={date}")
        data = request.json or {}
        
        year, month, day = date.split('-')
        filepath = get_student_contact_book_path(student_id, int(year), int(month))
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'Contact book not found'}), 404
        
        with open(filepath, 'r', encoding='utf-8') as f:
            contact_book = json.load(f)
        
        record = next((r for r in contact_book['records'] if r['date'] == date), None)
        if not record:
            return jsonify({'error': 'Record not found'}), 404
        
        # Update status to signed
        record['status'] = 'signed'
        record['signedAt'] = data.get('signedAt') or __import__('datetime').datetime.now().isoformat()
        
        # Update parent note if provided
        if data.get('note'):
            if 'original' not in record:
                record['original'] = {'teacher': None, 'parent': None}
            if not record['original'].get('parent'):
                record['original']['parent'] = {}
            record['original']['parent']['note'] = data['note']
            record['original']['parent']['updatedAt'] = record['signedAt']
        
        contact_book['metadata']['lastModified'] = __import__('datetime').datetime.now().isoformat()
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(contact_book, f, ensure_ascii=False, indent=2)
        
        return jsonify({'status': 'signed'}), 200
    except Exception as e:
        print(f"[CONTACT_BOOK SIGN] Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@contact_book_bp.route('/<student_id>/<date>/items-checked', methods=['PUT'])
def update_items_checked(student_id, date):
    """Update the checked items list"""
    try:
        print(f"[CONTACT_BOOK ITEMS] student_id={student_id}, date={date}")
        data = request.json or {}
        
        year, month, day = date.split('-')
        filepath = get_student_contact_book_path(student_id, int(year), int(month))
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'Contact book not found'}), 404
        
        with open(filepath, 'r', encoding='utf-8') as f:
            contact_book = json.load(f)
        
        record = next((r for r in contact_book['records'] if r['date'] == date), None)
        if not record:
            return jsonify({'error': 'Record not found'}), 404
        
        # Update checked items
        if record.get('itemsToBring'):
            record['itemsToBring']['checkedItems'] = data.get('checkedItems', [])
            record['itemsToBring']['checkedAt'] = data.get('checkedAt') or __import__('datetime').datetime.now().isoformat()
        
        contact_book['metadata']['lastModified'] = __import__('datetime').datetime.now().isoformat()
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(contact_book, f, ensure_ascii=False, indent=2)
        
        return jsonify({'status': 'updated'}), 200
    except Exception as e:
        print(f"[CONTACT_BOOK ITEMS] Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@contact_book_bp.route('/<student_id>/<date>/comments', methods=['GET', 'POST'])
def handle_comments(student_id, date):
    """Get or add comments for a contact book entry"""
    try:
        year, month, day = date.split('-')
        filepath = get_student_contact_book_path(student_id, int(year), int(month))
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'Contact book not found'}), 404
        
        with open(filepath, 'r', encoding='utf-8') as f:
            contact_book = json.load(f)
        
        record = next((r for r in contact_book['records'] if r['date'] == date), None)
        if not record:
            return jsonify({'error': 'Record not found'}), 404
        
        if request.method == 'GET':
            # Return comments for this date
            comments = record.get('comments', [])
            return jsonify(comments), 200
        
        elif request.method == 'POST':
            # Add a new comment
            data = request.json
            if not data or not data.get('content'):
                return jsonify({'error': 'Content is required'}), 400
            
            # Ensure comments array exists
            if 'comments' not in record:
                record['comments'] = []
            
            # Create comment object
            import datetime
            comment = {
                'senderId': data.get('senderId', 'parent'),
                'name': data.get('name', '家長'),
                'content': data['content'],
                'createdAt': datetime.datetime.now().isoformat()
            }
            
            record['comments'].append(comment)
            
            # Update metadata
            if 'metadata' not in contact_book:
                contact_book['metadata'] = {}
            contact_book['metadata']['lastModified'] = datetime.datetime.now().isoformat()
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(contact_book, f, ensure_ascii=False, indent=2)
            
            print(f"[CONTACT_BOOK COMMENT] Added comment for {student_id}/{date}")
            return jsonify(comment), 201
            
    except Exception as e:
        print(f"[CONTACT_BOOK COMMENT] Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

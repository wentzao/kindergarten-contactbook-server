from flask import Blueprint, request, jsonify
import os, json

journal_bp = Blueprint('journal', __name__)

# ── Shared DataService instance ──
from services.data_service import DataService
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)


@journal_bp.route('/<class_name>/<date>', methods=['GET'])
def get_journal(class_name, date):
    """Get class journal for a specific class and date."""
    journal = data_service.get_class_journal(class_name, date)
    if journal:
        return jsonify(journal)
    # Return empty structure if no journal exists yet
    return jsonify({
        'className': class_name,
        'date': date,
        'contentBlocks': [],
        'studentNotes': {},
        'editedBy': None,
        'updatedAt': None,
    })


@journal_bp.route('/<class_name>/<date>', methods=['PUT'])
def save_journal(class_name, date):
    """Save/update class journal (used by auto-save)."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    content_blocks = data.get('contentBlocks', [])
    student_notes = data.get('studentNotes', {})
    edited_by = data.get('editedBy')

    result = data_service.save_class_journal(
        class_name, date, content_blocks, student_notes, edited_by
    )
    return jsonify(result)


@journal_bp.route('/<class_name>/<date>', methods=['DELETE'])
def delete_journal(class_name, date):
    """Delete class journal."""
    deleted = data_service.delete_class_journal(class_name, date)
    if deleted:
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Journal not found'}), 404


@journal_bp.route('/student/<student_id>/<date>', methods=['GET'])
def get_journal_for_student(student_id, date):
    """Parent-facing: get class journal + individual note for a student on a date.
    Requires ?className= query param to identify the class."""
    class_name = request.args.get('className')
    if not class_name:
        return jsonify({'error': 'className query param required'}), 400

    journal = data_service.get_class_journal(class_name, date)
    if not journal:
        return jsonify({
            'date': date,
            'classJournal': None,
            'studentNote': None,
        })

    student_note = journal.get('studentNotes', {}).get(student_id)

    return jsonify({
        'date': date,
        'classJournal': {
            'contentBlocks': journal.get('contentBlocks', []),
            'updatedAt': journal.get('updatedAt'),
        },
        'studentNote': student_note,
    })

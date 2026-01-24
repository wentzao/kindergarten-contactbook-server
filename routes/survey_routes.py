from flask import Blueprint, request, jsonify
from services.data_service import DataService
from datetime import datetime
import os

survey_bp = Blueprint('survey', __name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
data_service = DataService(DATA_DIR)

@survey_bp.route('/available/<child_id>', methods=['GET'])
def get_available_surveys(child_id):
    """
    Get available surveys for a child.
    Query params:
      - class: Filter by class name (optional)
    Returns surveys with status: 'pending' or 'completed'
    """
    child_class = request.args.get('class', None)
    surveys = data_service.get_available_surveys(child_class)
    
    # Add status for each survey based on whether child has responded
    result = []
    for survey in surveys:
        status = data_service.get_child_survey_status(survey['id'], child_id)
        survey_with_status = {**survey, 'status': status}
        result.append(survey_with_status)
    
    return jsonify(result)

@survey_bp.route('/<survey_id>', methods=['GET'])
def get_survey_details(survey_id):
    """
    Get survey details by ID.
    Query params:
      - childId: To check if this child has completed the survey (optional)
    """
    child_id = request.args.get('childId', None)
    survey = data_service.get_survey_definition(survey_id)
    
    if not survey:
        return jsonify({'error': 'Survey not found'}), 404
    
    # Add status if child_id is provided
    if child_id:
        status = data_service.get_child_survey_status(survey_id, child_id)
        survey = {**survey, 'status': status}
    
    return jsonify(survey)

@survey_bp.route('/<survey_id>/response/<child_id>', methods=['GET'])
def get_survey_response(survey_id, child_id):
    """
    Get existing survey response for a child.
    Returns the previous answers if exists, or null if not filled.
    """
    response = data_service.get_survey_response(survey_id, child_id)
    if response:
        return jsonify(response)
    return jsonify(None)

@survey_bp.route('/responses', methods=['POST'])
def submit_survey_response():
    data = request.json
    if not data or 'surveyId' not in data or 'childId' not in data:
        return jsonify({'error': 'Missing required fields'}), 400
    
    survey_id = data['surveyId']
    child_id = data['childId']
    answers = data.get('answers', {})
    user_id = data.get('userId', '')  # Record who submitted
    
    # Server-side deadline validation (prevents front-end date manipulation)
    survey = data_service.get_survey_definition(survey_id)
    if not survey:
        return jsonify({'error': 'Survey not found'}), 404
    
    due_date = survey.get('dueDate')
    if due_date:
        try:
            deadline = datetime.strptime(due_date, '%Y-%m-%d').date()
            today = datetime.now().date()
            if today > deadline:
                return jsonify({'error': '問卷已截止，無法提交', 'code': 'SURVEY_EXPIRED'}), 400
        except ValueError:
            pass  # Invalid date format, skip validation
    
    success = data_service.save_survey_response(survey_id, child_id, answers, user_id)
    if success:
        return jsonify({'status': 'success'}), 201
    return jsonify({'error': 'Failed to save'}), 500


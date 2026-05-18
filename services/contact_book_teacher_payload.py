"""Helpers for contact-book teacher payload preservation.

The teacher note editor can write through REST or live collaboration. Both paths
should normalize block-derived text and avoid replacing existing visible content
with an empty transient payload.
"""
import re


def _trimmed(value):
    return value.strip() if isinstance(value, str) else ''


def _html_to_text(value):
    if not isinstance(value, str):
        return ''
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(value, 'html.parser').get_text('\n').strip()
    except Exception:
        text = re.sub(r'<[^>]+>', '', value)
        return text.strip()


def _text_from_blocks(blocks):
    if not isinstance(blocks, list):
        return ''

    text_parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get('type')
        content = block.get('content')
        if block_type == 'text':
            text = _html_to_text(content)
        elif block_type == 'plaintext':
            text = _trimmed(content)
        else:
            continue
        if text:
            text_parts.append(text)
    return '\n'.join(text_parts)


def _block_has_visible_content(block):
    if not isinstance(block, dict):
        return False

    block_type = block.get('type')
    if block_type == 'text':
        return bool(_html_to_text(block.get('content')))
    if block_type == 'plaintext':
        return bool(_trimmed(block.get('content')))
    if block_type in ('image', 'link'):
        return bool(_trimmed(block.get('url')))
    if block_type == 'video':
        return bool(_trimmed(block.get('youtubeUrl')))
    if block_type == 'survey':
        return bool(_trimmed(block.get('surveyId')))
    if block_type in ('activity', 'bring', 'return'):
        items = block.get('items')
        return isinstance(items, list) and any(_trimmed(item) for item in items)
    return any(_trimmed(block.get(key)) for key in ('content', 'caption', 'url', 'title'))


def normalize_teacher_payload(payload):
    normalized = dict(payload or {})
    blocks = normalized.get('blocks')
    if isinstance(blocks, list):
        derived_note = _text_from_blocks(blocks)
        if derived_note and not _trimmed(normalized.get('note')):
            normalized['note'] = derived_note
    return normalized


def teacher_payload_has_visible_content(
    payload,
    items_to_bring=None,
    returned_items=None,
    attached_items=None,
    survey_id=None,
):
    payload = payload or {}
    if _trimmed(payload.get('note')):
        return True

    blocks = payload.get('blocks')
    if isinstance(blocks, list) and any(_block_has_visible_content(block) for block in blocks):
        return True

    for values in (items_to_bring, returned_items, attached_items):
        if isinstance(values, list) and any(_trimmed(item) for item in values):
            return True

    return bool(_trimmed(survey_id))


def merge_existing_visible_content_if_empty(
    incoming_payload,
    existing_payload,
    items_to_bring=None,
    returned_items=None,
    attached_items=None,
    survey_id=None,
):
    incoming = normalize_teacher_payload(incoming_payload)
    existing = normalize_teacher_payload(existing_payload)

    if teacher_payload_has_visible_content(
        incoming,
        items_to_bring=items_to_bring,
        returned_items=returned_items,
        attached_items=attached_items,
        survey_id=survey_id,
    ):
        return incoming, False

    if not teacher_payload_has_visible_content(existing):
        return incoming, False

    merged = dict(incoming)
    for key in ('note', 'blocks'):
        value = existing.get(key)
        if value not in (None, '', []):
            merged[key] = value
    return merged, True

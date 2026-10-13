# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import copy
from app.detectors.pipeline import detector_pipeline
from app.policy import policy_engine
from app.sessions import session_manager

async def pseudonymize_text(session_id: str, text: str, policy_name: str='standard'):
    if not text:
        return (text, {})
    detections = await detector_pipeline.detect(text)
    selected = await policy_engine.select(session_id, detections, text, policy_name)
    if not selected:
        return (text, {})
    replacements = []
    stats = {}
    for item in sorted(selected, key=lambda detection: (detection.start, detection.end)):
        token = await session_manager.tokenize(session_id, item.entity_type, item.text)
        replacements.append((item.start, item.end, token, item.entity_type))
        stats[item.entity_type] = stats.get(item.entity_type, 0) + 1
    result = text
    for start, end, token, _entity_type in sorted(replacements, key=lambda item: (item[0], item[1]), reverse=True):
        result = result[:start] + token + result[end:]
    return (result, stats)

async def _pseudonymize_content(session_id: str, content, policy_name: str='standard'):
    stats = {}
    if isinstance(content, str):
        return await pseudonymize_text(session_id, content, policy_name)
    if isinstance(content, list):
        result = copy.deepcopy(content)
        for part in result:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get('text'), str):
                new_text, part_stats = await pseudonymize_text(session_id, part['text'], policy_name)
                part['text'] = new_text
                for key, value in part_stats.items():
                    stats[key] = stats.get(key, 0) + value
        return (result, stats)
    return (content, stats)

async def pseudonymize_payload(session_id: str, payload: dict, policy_name: str='standard'):
    sanitized = copy.deepcopy(payload)
    total_stats = {}
    messages = sanitized.get('messages', [])
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        new_content, stats = await _pseudonymize_content(session_id, content, policy_name)
        message['content'] = new_content
        for key, value in stats.items():
            total_stats[key] = total_stats.get(key, 0) + value
    return (sanitized, total_stats)

async def restore_object(session_id: str, value):
    if isinstance(value, str):
        return await session_manager.restore_text(session_id, value)
    if isinstance(value, list):
        return [await restore_object(session_id, item) for item in value]
    if isinstance(value, dict):
        restored = {}
        for key, item in value.items():
            restored[key] = await restore_object(session_id, item)
        return restored
    return value

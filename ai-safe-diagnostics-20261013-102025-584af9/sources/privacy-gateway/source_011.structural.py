# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import httpx
from app.config import settings

class CloudEgressPolicyError(ValueError):
    pass
CLOUD_MESSAGE_ROLES = {'system', 'developer', 'user', 'assistant'}
CLOUD_FLOAT_FIELDS = {'temperature', 'top_p', 'presence_penalty', 'frequency_penalty'}
CLOUD_REASONING_EFFORTS = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}

def upstream_headers():
    return {'Authorization': f'Bearer {settings.litellm_api_key}', 'Content-Type': 'application/json'}

def _cloud_messages(payload: dict) -> list[dict]:
    messages = payload.get('messages')
    if not isinstance(messages, list):
        raise CloudEgressPolicyError('Cloud request requires a messages list')
    if not messages:
        raise CloudEgressPolicyError('Cloud request has no messages')
    if len(messages) > settings.cloud_hard_max_messages:
        raise CloudEgressPolicyError('Cloud hard message limit exceeded')
    safe_messages = []
    total_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            raise CloudEgressPolicyError('Invalid cloud message')
        role = message.get('role')
        if role not in CLOUD_MESSAGE_ROLES:
            raise CloudEgressPolicyError(f'Unsupported cloud message role: {role}')
        content = message.get('content')
        if not isinstance(content, str):
            raise CloudEgressPolicyError('Cloud egress currently supports text-only content')
        total_chars += len(content)
        if total_chars > settings.cloud_hard_max_input_chars:
            raise CloudEgressPolicyError('Cloud hard input size limit exceeded')
        safe_messages.append({'role': role, 'content': content})
    return safe_messages

def _copy_float_field(source: dict, destination: dict, key: str):
    if key not in source:
        return
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CloudEgressPolicyError(f'Invalid cloud generation field: {key}')
    destination[key] = value

def _copy_reasoning_effort(source: dict, destination: dict):
    if 'reasoning_effort' not in source:
        return None
    value = source['reasoning_effort']
    if not isinstance(value, str):
        raise CloudEgressPolicyError('Invalid cloud reasoning_effort')
    value = value.strip().lower()
    if value not in CLOUD_REASONING_EFFORTS:
        raise CloudEgressPolicyError('Unsupported cloud reasoning_effort')
    destination['reasoning_effort'] = value
    return value

def _copy_seed(source: dict, destination: dict):
    if 'seed' not in source:
        return
    value = source['seed']
    if isinstance(value, bool) or not isinstance(value, int):
        raise CloudEgressPolicyError('Invalid cloud generation field: seed')
    destination['seed'] = value

def _copy_n(source: dict, destination: dict):
    if 'n' not in source:
        return
    value = source['n']
    if isinstance(value, bool) or not isinstance(value, int):
        raise CloudEgressPolicyError('Invalid cloud generation field: n')
    if value != 1:
        raise CloudEgressPolicyError('Cloud n must equal 1')
    destination['n'] = 1

def _completion_limit(source: dict, destination: dict):
    has_old = 'max_tokens' in source
    has_new = 'max_completion_tokens' in source
    if has_old and has_new:
        raise CloudEgressPolicyError('Specify only one completion token limit')
    if has_new:
        value = source['max_completion_tokens']
    elif has_old:
        value = source['max_tokens']
    else:
        value = settings.cloud_default_completion_tokens
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CloudEgressPolicyError('Invalid cloud completion token limit')
    if value > settings.cloud_hard_max_completion_tokens:
        raise CloudEgressPolicyError('Cloud hard completion token limit exceeded')
    destination['max_completion_tokens'] = value

def prepare_upstream_payload(payload: dict, upstream_model: str, route_class: str) -> dict:
    if route_class == 'local':
        result = dict(payload)
        result['model'] = upstream_model
        return result
    if route_class != 'cloud':
        raise CloudEgressPolicyError('Unknown route class')
    if not settings.cloud_egress_enabled:
        raise CloudEgressPolicyError('Cloud egress is disabled')
    result = {'model': upstream_model, 'messages': _cloud_messages(payload)}
    stream = payload.get('stream', False)
    if not isinstance(stream, bool):
        raise CloudEgressPolicyError('Invalid stream field')
    result['stream'] = stream
    reasoning_effort = _copy_reasoning_effort(payload, result)
    if reasoning_effort:
        result['allowed_openai_params'] = ['reasoning_effort']
    for key in CLOUD_FLOAT_FIELDS:
        if reasoning_effort and reasoning_effort != 'none' and (key in {'temperature', 'top_p'}):
            continue
        _copy_float_field(payload, result, key)
    _copy_seed(payload, result)
    _copy_n(payload, result)
    _completion_limit(payload, result)
    return result

async def check_upstream():
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f'{settings.litellm_base_url}/models', headers=upstream_headers())
        return response.status_code

async def completion(payload: dict, upstream_model: str):
    public_model = payload.get('model')
    if not settings.has_model(public_model):
        raise ValueError('Unknown public model')
    route_class = settings.route_for(public_model)
    prepared_payload = prepare_upstream_payload(payload, upstream_model, route_class)
    client = httpx.AsyncClient(timeout=None)
    request = client.build_request('POST', f'{settings.litellm_base_url}/chat/completions', headers=upstream_headers(), json=prepared_payload)
    response = await client.send(request, stream=bool(prepared_payload.get('stream', False)))
    return (client, response)

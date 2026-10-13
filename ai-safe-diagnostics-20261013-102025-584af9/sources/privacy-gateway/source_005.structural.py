# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from app.budget import CloudBudgetError, assess_cloud_request
from app.config import settings
from app.privacy import pseudonymize_payload, restore_object
from app.security import require_gateway_key
from app.sessions import session_manager
from app.streaming import transform_openai_stream
from app.upstream import CloudEgressPolicyError, check_upstream, completion
SESSION_ID_RE = re.compile('^[A-Za-z0-9._:\\-]{1,128}$')
CONTINUE_RE = re.compile('^KONTYNUUJ\\s+([A-Z0-9]{8})$', re.IGNORECASE)
LOCAL_APPROVAL_PREFIX = '[LOCAL CLOUD APPROVAL]'

@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(session_manager.cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
app = FastAPI(title='AI Privacy Gateway', version='0.6.7', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

def get_session_id(request: Request):
    session_id = request.headers.get('X-Privacy-Session-ID')
    if not session_id:
        return uuid.uuid4().hex
    if not SESSION_ID_RE.fullmatch(session_id):
        return None
    return session_id

def privacy_headers(session_id: str, stats: dict, privacy_policy: str):
    entities = ','.join((f'{key}={value}' for key, value in sorted(stats.items())))
    return {'X-Privacy-Session-ID': session_id, 'X-Privacy-Enabled': 'true', 'X-Privacy-Policy': privacy_policy, 'X-Privacy-Entities': entities or 'none'}

def _last_user_text(payload: dict):
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return content
        return None
    return None

def _parse_control_command(text: str | None):
    if not isinstance(text, str):
        return (None, None)
    stripped = text.strip()
    if stripped.upper() == 'ANULUJ':
        return ('cancel', None)
    match = CONTINUE_RE.fullmatch(stripped)
    if match:
        return ('continue', match.group(1).upper())
    return (None, None)

def _is_local_control_message(message: dict) -> bool:
    if not isinstance(message, dict):
        return False
    role = message.get('role')
    content = message.get('content')
    if not isinstance(content, str):
        return False
    if role == 'assistant' and content.startswith(LOCAL_APPROVAL_PREFIX):
        return True
    if role == 'user':
        command, _ = _parse_control_command(content)
        return command is not None
    return False

def _strip_local_control_messages(payload: dict) -> dict:
    result = dict(payload)
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return result
    result['messages'] = [message for message in messages if not _is_local_control_message(message)]
    return result

def _local_completion_payload(requested_model: str, content: str):
    return {'id': 'chatcmpl-local-' + uuid.uuid4().hex, 'object': 'chat.completion', 'created': int(time.time()), 'model': requested_model, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}

def _local_completion_response(requested_model: str, content: str, stream: bool, headers: dict):
    if not stream:
        return Response(content=json.dumps(_local_completion_payload(requested_model, content), ensure_ascii=False), status_code=200, media_type='application/json', headers=headers)
    response_id = 'chatcmpl-local-' + uuid.uuid4().hex
    created = int(time.time())

    async def event_stream():
        first = {'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': requested_model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content}, 'finish_reason': None}]}
        final = {'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': requested_model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}
        yield ('data: ' + json.dumps(first, ensure_ascii=False) + '\n\n')
        yield ('data: ' + json.dumps(final, ensure_ascii=False) + '\n\n')
        yield 'data: [DONE]\n\n'
    return StreamingResponse(event_stream(), status_code=200, media_type='text/event-stream', headers={**headers, 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

def _approval_text(estimate: dict, code: str):
    reasons = ', '.join(estimate['soft_reasons'])
    return f'{LOCAL_APPROVAL_PREFIX}\n\nZapytanie zostało zatrzymane lokalnie. Nic nie zostało jeszcze wysłane do chmury.\n\nOrientacyjny koszt przy wykorzystaniu pełnego limitu odpowiedzi: ${estimate['estimated_max_cost_usd']:.6f}\nSzacowane tokeny wejścia: {estimate['estimated_input_tokens']}\nMaksymalna liczba tokenów odpowiedzi: {estimate['max_output_tokens']}\nPowód zatrzymania: {reasons}\n\nAby jednorazowo zatwierdzić dokładnie to zapytanie, wpisz:\nKONTYNUUJ {code}\n\nAby je odrzucić, wpisz:\nANULUJ'

def _hard_block_text(estimate: dict):
    reasons = ', '.join(estimate['hard_reasons'])
    return f'[LOCAL CLOUD LIMIT]\n\nZapytanie nie zostało wysłane do chmury, ponieważ przekracza twardy limit bezpieczeństwa.\n\nPowód: {reasons}\nSzacowane tokeny wejścia: {estimate['estimated_input_tokens']}\nMaksymalna liczba tokenów odpowiedzi: {estimate['max_output_tokens']}\nOrientacyjny koszt przy wykorzystaniu pełnego limitu odpowiedzi: ${estimate['estimated_max_cost_usd']:.6f}'

def _invalid_approval_text(status: str):
    if status == 'code_mismatch':
        detail = 'Kod zatwierdzenia jest nieprawidłowy.'
    elif status == 'model_mismatch':
        detail = 'Zatwierdzenie dotyczy innego modelu.'
    elif status == 'expired':
        detail = 'Zatwierdzenie wygasło.'
    else:
        detail = 'Nie ma aktywnego zapytania oczekującego na zatwierdzenie.'
    return f'{LOCAL_APPROVAL_PREFIX}\n\n{detail}\nNic nie zostało wysłane do chmury.'

@app.get('/health')
async def health():
    try:
        status = await check_upstream()
        return {'status': 'ok' if status == 200 else 'degraded', 'gateway': 'ok', 'privacy_engine': 'enabled', 'privacy_streaming': 'enabled', 'models': settings.public_models, 'model_policies': {model_id: settings.policy_for(model_id) for model_id in settings.public_models}, 'model_routes': {model_id: settings.route_for(model_id) for model_id in settings.public_models}, 'cloud_approval_gate': 'enabled' if settings.cloud_approval_enabled else 'disabled', 'session_ttl_seconds': settings.session_ttl_seconds, 'upstream_http_status': status}
    except Exception:
        return JSONResponse(status_code=503, content={'status': 'error', 'gateway': 'ok', 'upstream': 'unreachable'})

@app.get('/v1/models', dependencies=[Depends(require_gateway_key)])
async def models():
    return {'object': 'list', 'data': [{'id': model_id, 'object': 'model', 'created': int(time.time()), 'owned_by': 'privacy-gateway'} for model_id in settings.public_models]}

@app.get('/v1/models/{model_id}', dependencies=[Depends(require_gateway_key)])
async def model(model_id: str):
    if not settings.has_model(model_id):
        return JSONResponse(status_code=404, content={'error': {'message': 'Model not found', 'type': 'invalid_request_error', 'code': 'model_not_found'}})
    return {'id': model_id, 'object': 'model', 'created': int(time.time()), 'owned_by': 'privacy-gateway'}

@app.get('/v1/privacy/sessions/{session_id}', dependencies=[Depends(require_gateway_key)])
async def privacy_session_status(session_id: str):
    if not SESSION_ID_RE.fullmatch(session_id):
        return JSONResponse(status_code=400, content={'error': 'Invalid session id'})
    status = await session_manager.status(session_id)
    if status is None:
        return JSONResponse(status_code=404, content={'status': 'not_found_or_expired'})
    return status

@app.delete('/v1/privacy/sessions/{session_id}', dependencies=[Depends(require_gateway_key)])
async def destroy_privacy_session(session_id: str):
    if not SESSION_ID_RE.fullmatch(session_id):
        return JSONResponse(status_code=400, content={'error': 'Invalid session id'})
    destroyed = await session_manager.destroy(session_id)
    return {'session_id': session_id, 'destroyed': destroyed}

@app.post('/v1/chat/completions', dependencies=[Depends(require_gateway_key)])
async def chat_completions(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON body', 'type': 'invalid_request_error'}})
    requested_model = payload.get('model')
    if not settings.has_model(requested_model):
        return JSONResponse(status_code=400, content={'error': {'message': f'Unsupported model: {requested_model}', 'type': 'invalid_request_error', 'code': 'model_not_found'}})
    stream_requested = payload.get('stream', False)
    if not isinstance(stream_requested, bool):
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid stream field', 'type': 'invalid_request_error'}})
    privacy_policy = settings.policy_for(requested_model)
    route_class = settings.route_for(requested_model)
    session_id = get_session_id(request)
    if session_id is None:
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid X-Privacy-Session-ID', 'type': 'invalid_request_error'}})
    await session_manager.get_or_create(session_id)
    upstream_model = settings.upstream_for(requested_model)
    stats = {}
    if route_class == 'cloud':
        command, code = _parse_control_command(_last_user_text(payload))
        if command == 'cancel':
            cancelled = await session_manager.cancel_cloud_approval(session_id)
            if cancelled:
                text = f'{LOCAL_APPROVAL_PREFIX}\n\nOczekujące zapytanie zostało anulowane. Nic nie zostało wysłane do chmury.'
            else:
                text = f'{LOCAL_APPROVAL_PREFIX}\n\nNie ma aktywnego zapytania oczekującego na anulowanie.'
            return _local_completion_response(requested_model, text, stream_requested, {**privacy_headers(session_id, {}, privacy_policy), 'X-Cloud-Approval': 'cancelled'})
        if command == 'continue':
            status, pending = await session_manager.consume_cloud_approval(session_id, requested_model, code)
            if status != 'ok' or pending is None:
                return _local_completion_response(requested_model, _invalid_approval_text(status), stream_requested, {**privacy_headers(session_id, {}, privacy_policy), 'X-Cloud-Approval': 'invalid'})
            sanitized_payload = pending['sanitized_payload']
            sanitized_payload['stream'] = stream_requested
            stats = pending['privacy_stats']
            upstream_model = pending['upstream_model']
        else:
            await session_manager.cancel_cloud_approval(session_id)
            payload = _strip_local_control_messages(payload)
            sanitized_payload, stats = await pseudonymize_payload(session_id, payload, privacy_policy)
            try:
                estimate = assess_cloud_request(sanitized_payload, requested_model)
            except CloudBudgetError as exc:
                return JSONResponse(status_code=400, content={'error': {'message': str(exc), 'type': 'invalid_request_error', 'code': 'cloud_budget_error'}}, headers=privacy_headers(session_id, stats, privacy_policy))
            if estimate['hard_block']:
                return _local_completion_response(requested_model, _hard_block_text(estimate), stream_requested, {**privacy_headers(session_id, stats, privacy_policy), 'X-Cloud-Hard-Block': 'true', 'X-Cloud-Estimated-Cost-USD': f'{estimate['estimated_max_cost_usd']:.6f}'})
            if estimate['approval_required']:
                code = await session_manager.create_cloud_approval(session_id, requested_model, upstream_model, privacy_policy, sanitized_payload, stats, estimate)
                return _local_completion_response(requested_model, _approval_text(estimate, code), stream_requested, {**privacy_headers(session_id, stats, privacy_policy), 'X-Cloud-Approval-Required': 'true', 'X-Cloud-Estimated-Cost-USD': f'{estimate['estimated_max_cost_usd']:.6f}', 'X-Cloud-Estimated-Input-Tokens': str(estimate['estimated_input_tokens']), 'X-Cloud-Max-Output-Tokens': str(estimate['max_output_tokens'])})
    else:
        sanitized_payload, stats = await pseudonymize_payload(session_id, payload, privacy_policy)
    try:
        client, upstream = await completion(sanitized_payload, upstream_model)
    except CloudEgressPolicyError as exc:
        return JSONResponse(status_code=400, content={'error': {'message': str(exc), 'type': 'invalid_request_error', 'code': 'cloud_egress_rejected'}}, headers=privacy_headers(session_id, stats, privacy_policy))
    except Exception:
        return JSONResponse(status_code=502, content={'error': {'message': 'Upstream model unavailable', 'type': 'upstream_error'}}, headers=privacy_headers(session_id, stats, privacy_policy))
    if sanitized_payload.get('stream', False):
        if upstream.status_code >= 400:
            try:
                body = await upstream.aread()
                return Response(content=body, status_code=upstream.status_code, media_type=upstream.headers.get('content-type', 'application/json'), headers=privacy_headers(session_id, stats, privacy_policy))
            finally:
                await upstream.aclose()
                await client.aclose()

        async def safe_stream():
            try:
                async for chunk in transform_openai_stream(upstream, session_id, requested_model):
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()
        return StreamingResponse(safe_stream(), status_code=200, media_type='text/event-stream', headers={**privacy_headers(session_id, stats, privacy_policy), 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    try:
        body = await upstream.aread()
        if upstream.status_code >= 400:
            return Response(content=body, status_code=upstream.status_code, media_type=upstream.headers.get('content-type', 'application/json'), headers=privacy_headers(session_id, stats, privacy_policy))
        try:
            data = json.loads(body)
        except Exception:
            return JSONResponse(status_code=502, content={'error': {'message': 'Invalid upstream response', 'type': 'upstream_error'}}, headers=privacy_headers(session_id, stats, privacy_policy))
        restored = await restore_object(session_id, data)
        restored['model'] = requested_model
        return Response(content=json.dumps(restored, ensure_ascii=False), status_code=200, media_type='application/json', headers=privacy_headers(session_id, stats, privacy_policy))
    finally:
        await upstream.aclose()
        await client.aclose()

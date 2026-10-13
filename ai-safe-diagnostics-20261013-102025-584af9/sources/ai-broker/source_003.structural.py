# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import copy
import json
import os
import re
import time
import uuid
from typing import Optional
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from app.learning import learning_system
from app.verification import VALID_REASONING_EFFORTS, VALID_VERIFY_MODES, build_verification_payload, choose_verification_effort, completion_tokens_for_effort, verification_cache
VERSION = '0.2.10'

def env_int(name: str, default: int, minimum: int=1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f'{name} must be integer') from exc
    if value < minimum:
        raise RuntimeError(f'{name} must be >= {minimum}')
    return value
BROKER_API_KEY = os.getenv('BROKER_API_KEY', '')
PRIVACY_GATEWAY_API_KEY = os.getenv('PRIVACY_GATEWAY_API_KEY', '')
OLLAMA_BASE_URL = os.getenv('OLLAMA_BASE_URL', 'http://host.docker.internal:11434').rstrip('/')
PRIVACY_GATEWAY_BASE_URL = os.getenv('PRIVACY_GATEWAY_BASE_URL', 'http://privacy-gateway:8088/v1').rstrip('/')
PRIMARY_MODEL = os.getenv('BROKER_PRIMARY_MODEL', 'gemma3-12b-16k')
ROUTER_MODEL = os.getenv('BROKER_ROUTER_MODEL', 'qwen2.5:1.5b')
CLOUD_MODEL = os.getenv('BROKER_CLOUD_MODEL', 'secure-openai')
CLOUD_MODE = os.getenv('BROKER_CLOUD_MODE', 'auto').strip().lower()
LOCAL_MAX_INPUT_CHARS = env_int('<OPAQUE_VALUE_REDACTED>', 36000)
LOCAL_DEFAULT_MAX_TOKENS = env_int('<OPAQUE_VALUE_REDACTED>', 2048)
LOCAL_MAX_TOKENS = env_int('BROKER_LOCAL_MAX_TOKENS', 4096)
LOCAL_CONCISE_MAX_TOKENS = env_int('<OPAQUE_VALUE_REDACTED>', 256)
ROUTER_SAMPLE_CHARS = env_int('BROKER_ROUTER_SAMPLE_CHARS', 6000)
ROUTER_NUM_CTX = env_int('BROKER_ROUTER_NUM_CTX', 2048)
ROUTER_NUM_PREDICT = env_int('BROKER_ROUTER_NUM_PREDICT', 128)
ROUTER_TIMEOUT_SECONDS = env_int('<OPAQUE_VALUE_REDACTED>', 60)
CAPABILITY_SAMPLE_CHARS = env_int('<OPAQUE_VALUE_REDACTED>', 6000)
CAPABILITY_NUM_PREDICT = env_int('<OPAQUE_VALUE_REDACTED>', 192)
CAPABILITY_TIMEOUT_SECONDS = env_int('<OPAQUE_VALUE_REDACTED>', 180)
UPSTREAM_TIMEOUT_SECONDS = env_int('<OPAQUE_VALUE_REDACTED>', 900)
CLOUD_HARD_MAX_TOKENS = env_int('<OPAQUE_VALUE_REDACTED>', 16384)
EXTERNAL_MODEL_LABELS_RAW = os.getenv('BROKER_EXTERNAL_MODEL_LABELS_JSON', '{"secure-openai":"OpenAI · GPT-6 Luna"}')
try:
    EXTERNAL_MODEL_LABELS = json.loads(EXTERNAL_MODEL_LABELS_RAW)
except json.JSONDecodeError:
    EXTERNAL_MODEL_LABELS = {'secure-openai': 'OpenAI · GPT-6 Luna'}
if not isinstance(EXTERNAL_MODEL_LABELS, dict):
    EXTERNAL_MODEL_LABELS = {}
if not BROKER_API_KEY:
    raise RuntimeError('BROKER_API_KEY is not configured')
if not PRIVACY_GATEWAY_API_KEY:
    raise RuntimeError('PRIVACY_GATEWAY_API_KEY is not configured')
if CLOUD_MODE not in {'auto', 'local-only', 'cloud-only'}:
    raise RuntimeError('BROKER_CLOUD_MODE must be auto, local-only or cloud-only')
SESSION_ID_RE = re.compile('^[A-Za-z0-9._:\\-]{1,128}$')
CONTINUE_RE = re.compile('^\\s*KONTYNUUJ\\s+([A-Za-z0-9]{8})\\s*[.!]?\\s*$', re.IGNORECASE)
CANCEL_RE = re.compile('^\\s*ANULUJ\\s*[.!]?\\s*$', re.IGNORECASE)
APPROVAL_MARKER = '[LOCAL CLOUD APPROVAL]'
BROKER_BANNER_RE = re.compile('^\\[(?:LOCAL|CLOUD) · [^\\]]+\\]\\n\\n')
app = FastAPI(title='AI Broker', version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)

def local_control_response(payload: dict, content: str):
    completion_id = 'chatcmpl-local-control-' + uuid.uuid4().hex
    created = int(time.time())
    stream = bool(payload.get('stream', False))
    if not stream:
        return JSONResponse(content={'id': completion_id, 'object': 'chat.completion', 'created': created, 'model': 'secure-auto', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}, headers={'X-AI-Broker-Route': 'local', 'X-AI-Broker-Reason': 'learning_control'})

    async def events():
        first = {'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': 'secure-auto', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content}, 'finish_reason': None}]}
        final = {'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': 'secure-auto', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}
        yield ('data: ' + json.dumps(first, ensure_ascii=False, separators=(',', ':')) + '\n\n').encode('utf-8')
        yield ('data: ' + json.dumps(final, ensure_ascii=False, separators=(',', ':')) + '\n\n').encode('utf-8')
        yield b''
    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'X-AI-Broker-Route': 'local', 'X-AI-Broker-Reason': 'learning_control'})

def require_broker_key(request: Request):
    expected = 'Bearer ' + BROKER_API_KEY
    if request.headers.get('Authorization', '') != expected:
        raise HTTPException(status_code=401, detail='Unauthorized')

def session_id_from_request(request: Request) -> Optional[str]:
    value = request.headers.get('X-Privacy-Session-ID')
    if not value or not SESSION_ID_RE.fullmatch(value):
        return None
    return value

def text_from_content(content):
    texts = []
    has_image = False
    if isinstance(content, str):
        return (content, False)
    if not isinstance(content, list):
        return ('', False)
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get('type', '')).lower()
        if part_type in {'image', 'image_url', 'input_image'} or 'image_url' in part:
            has_image = True
        text = part.get('text')
        if isinstance(text, str):
            texts.append(text)
        input_text = part.get('input_text')
        if isinstance(input_text, str):
            texts.append(input_text)
    return ('\n'.join(texts), has_image)

def latest_user_text(messages) -> str:
    if not isinstance(messages, list):
        return ''
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'user':
            text, _ = text_from_content(message.get('content'))
            return text.strip()
    return ''

def cloud_control_command(messages) -> bool:
    text = latest_user_text(messages)
    return bool(text and (CONTINUE_RE.fullmatch(text) or CANCEL_RE.fullmatch(text)))

def cloud_control_action(messages) -> Optional[str]:
    text = latest_user_text(messages)
    if not text:
        return None
    if CONTINUE_RE.fullmatch(text):
        return 'continue'
    if CANCEL_RE.fullmatch(text):
        return 'cancel'
    return None

def strip_broker_banner_text(value: str) -> str:
    return BROKER_BANNER_RE.sub('', value, count=1)

def strip_broker_banners(payload: dict) -> dict:
    result = copy.deepcopy(payload)
    messages = result.get('messages', [])
    if not isinstance(messages, list):
        return result
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        content = message.get('content')
        if isinstance(content, str):
            message['content'] = strip_broker_banner_text(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            for key in ('text', 'input_text'):
                value = part.get(key)
                if isinstance(value, str):
                    part[key] = strip_broker_banner_text(value)
    return result

def inspect_messages(messages):
    total_chars = 0
    message_count = 0
    has_image = False
    current_user_text = ''
    excerpts = []
    if not isinstance(messages, list):
        messages = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_count += 1
        role = str(message.get('role', ''))
        text, image = text_from_content(message.get('content'))
        total_chars += len(text)
        has_image = has_image or image
        if role == 'user' and text:
            current_user_text = text
        if text:
            excerpts.append(f'{role}: {text}')
    combined = '\n\n'.join(excerpts[-6:])
    if len(combined) > ROUTER_SAMPLE_CHARS:
        combined = combined[-ROUTER_SAMPLE_CHARS:]
    return {'total_chars': total_chars, 'message_count': message_count, 'has_image': has_image, 'latest_user_text': current_user_text[-ROUTER_SAMPLE_CHARS:], 'sample': combined}

def normalize_router_result(value):
    languages = {'pl', 'en', 'other'}
    tasks = {'simple', 'document', 'reasoning', 'coding', 'translation'}
    complexities = {'low', 'medium', 'high'}
    language = str(value.get('language', 'other')).lower()
    task = str(value.get('task', 'simple')).lower()
    complexity = str(value.get('complexity', 'medium')).lower()
    cloud_candidate = value.get('cloud_candidate', False)
    if language not in languages:
        language = 'other'
    if task not in tasks:
        task = 'simple'
    if complexity not in complexities:
        complexity = 'medium'
    if isinstance(cloud_candidate, str):
        cloud_candidate = cloud_candidate.strip().lower() == 'true'
    if complexity == 'low':
        cloud_candidate = False
    return {'language': language, 'task': task, 'complexity': complexity, 'cloud_candidate': bool(cloud_candidate)}

def _regex_hit_count(text: str, patterns) -> int:
    return sum((1 for pattern in patterns if re.search(pattern, text, re.IGNORECASE)))

def deterministic_5d(info: dict) -> dict:
    text = str(info.get('latest_user_text', ''))
    reasoning_hits = _regex_hit_count(text, ('\\bprzeanaliz\\w*', '\\banaliz\\w*', '\\breason\\w*', '\\bwyprowad\\w*', '\\bderive\\w*', '\\bporówn\\w*', '\\bcompare\\w*', '\\bocen\\w*', '\\bevaluat\\w*'))
    formal_hits = _regex_hit_count(text, ('\\bformal\\w*', '\\budowod\\w*', '\\bdow[oó]d\\w*', '\\bproof\\w*', '\\bprove\\w*', '\\btheorem\\w*', '\\binvariant\\w*', '\\bcorrectness\\w*', '\\bpoprawno\\w*', '\\bsafety\\b', '\\bliveness\\b', '\\brygorystycz\\w*', '\\brigorous\\w*'))
    edge_hits = _regex_hit_count(text, ('\\bsubtel\\w*', '\\bprzypad\\w*\\s+brzeg\\w*', '\\bedge\\s+case\\w*', '\\bcounterexample\\w*', '\\bkontrprzyk\\w*'))
    technical_hits = _regex_hit_count(text, ('\\basynchronic\\w*', '\\basynchron\\w*', '\\bbyzant\\w*', '\\bquorum\\w*', '\\bconsensus\\w*', '\\bequivocation\\w*', '\\bview\\s+change\\w*', '\\bpartial\\s+synchron\\w*', '\\bczęściow\\w*\\s+synchron\\w*', '\\brozproszon\\w*', '\\bdistributed\\w*', '\\blineariz\\w*', '\\bserializ\\w*', '\\bdeadlock\\w*', '\\brace\\s+condition\\w*', '\\bkryptograf\\w*', '\\bcryptograph\\w*', '\\bzero[- ]knowledge\\w*', '\\bformal\\s+verification\\w*', '\\btype\\s+system\\w*', '\\bcompiler\\w*', '\\bkernel\\w*', '\\bstochastic\\w*', '\\bbayesian\\w*', '\\bconvex\\w*', '\\beigen\\w*', '\\bdifferential\\s+equation\\w*', '\\bcausal\\s+inference\\w*'))
    action_hits = _regex_hit_count(text, ('\\bzaprojektuj\\b', '\\bprzeanalizuj\\b', '\\buwzględnij\\b', '\\bwskaż\\b', '\\buzasadnij\\b', '\\bporównaj\\b', '\\bwyjaśnij\\b', '\\boceń\\b', '\\bdesign\\b', '\\banaly[sz]e\\b', '\\binclude\\b', '\\bidentify\\b', '\\bjustify\\b', '\\bcompare\\b', '\\bexplain\\b', '\\bevaluate\\b'))
    precision_hits = _regex_hit_count(text, ('\\bdokładn\\w*', '\\bexact\\w*', '\\bformal\\w*', '\\budowod\\w*', '\\bdow[oó]d\\w*', '\\bproof\\w*', '\\bcorrectness\\w*', '\\bpoprawno\\w*', '\\binvariant\\w*', '\\bsafety\\b', '\\bliveness\\b', '\\bwarunk\\w*', '\\bcondition\\w*', '\\bsubtel\\w*', '\\bedge\\s+case\\w*'))
    reasoning_depth = 0
    if reasoning_hits >= 1:
        reasoning_depth += 1
    if formal_hits >= 1:
        reasoning_depth += 1
    if edge_hits >= 1:
        reasoning_depth += 1
    reasoning_depth = min(reasoning_depth, 3)
    if technical_hits >= 6:
        knowledge_depth = 3
    elif technical_hits >= 3:
        knowledge_depth = 2
    elif technical_hits >= 1:
        knowledge_depth = 1
    else:
        knowledge_depth = 0
    structural_requirements = action_hits + text.count(';') + min(text.count('\n-'), 4)
    if structural_requirements >= 7:
        constraint_density = 3
    elif structural_requirements >= 4:
        constraint_density = 2
    elif structural_requirements >= 2:
        constraint_density = 1
    else:
        constraint_density = 0
    if precision_hits >= 5:
        precision_requirement = 3
    elif precision_hits >= 3:
        precision_requirement = 2
    elif precision_hits >= 1:
        precision_requirement = 1
    else:
        precision_requirement = 0
    text_len = len(text)
    if text_len >= 6000 or action_hits >= 8:
        task_scope = 3
    elif text_len >= 3000 or action_hits >= 5:
        task_scope = 2
    elif text_len >= 1200 or action_hits >= 3:
        task_scope = 1
    else:
        task_scope = 0
    dimensions = {'reasoning_depth': reasoning_depth, 'knowledge_depth': knowledge_depth, 'constraint_density': constraint_density, 'precision_requirement': precision_requirement, 'task_scope': task_scope}
    score = sum(dimensions.values())
    proof_intent_hits = _regex_hit_count(text, ('\\budowod\\w*', '\\bdow[oó]d\\w*', '\\bproof\\w*', '\\bprove\\w*', '\\bwykaż\\w*', '\\bwykaz\\w*'))
    formal_technical_candidate = proof_intent_hits >= 1 and technical_hits >= 1
    capability_candidate = score >= 6 or (reasoning_depth >= 2 and precision_requirement >= 2) or (knowledge_depth >= 2 and task_scope >= 1) or formal_technical_candidate or (info.get('total_chars', 0) >= 12000)
    extreme = score >= 11 or (reasoning_depth == 3 and knowledge_depth >= 2 and (precision_requirement >= 2))
    return {**dimensions, 'score': score, 'capability_candidate': capability_candidate, 'extreme': extreme, 'evidence': {'reasoning_hits': reasoning_hits, 'formal_hits': formal_hits, 'edge_hits': edge_hits, 'technical_hits': technical_hits, 'action_hits': action_hits, 'precision_hits': precision_hits, 'proof_intent_hits': proof_intent_hits, 'formal_technical_candidate': formal_technical_candidate}}

async def classify_with_router(info: dict):
    system_prompt = '\nJesteś małym lokalnym PRE-ROUTEREM dla\nprywatnego systemu AI.\n\nNIE wykonuj zadania użytkownika. Tylko je\nsklasyfikuj.\n\nZwróć WYŁĄCZNIE JSON:\n\n{\n  "language": "pl|en|other",\n  "task": "simple|document|reasoning|coding|translation",\n  "complexity": "low|medium|high",\n  "cloud_candidate": true|false\n}\n\nZasady:\n- translation tylko gdy użytkownik naprawdę\n  prosi o tłumaczenie między językami.\n- formalny dowód, projekt protokołu,\n  wieloetapowa analiza z wieloma warunkami\n  lub subtelnymi przypadkami brzegowymi\n  zwykle oznacza complexity=high.\n- cloud_candidate=true tylko gdy\n  complexity=high i model lokalny klasy 12B\n  może być wyraźnie niewystarczający.\n- gdy complexity=low ustaw\n  cloud_candidate=false.\n- prywatność/PII sama w sobie NIE jest\n  powodem do chmury.\n- obrazy preferują lokalny model vision.\n'.strip()
    router_input = {'message_count': info['message_count'], 'total_chars': info['total_chars'], 'has_image': info['has_image'], 'content_sample': info['sample']}
    payload = {'model': ROUTER_MODEL, 'messages': [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': json.dumps(router_input, ensure_ascii=False)}], 'format': 'json', 'stream': False, 'keep_alive': -1, 'options': {'num_ctx': ROUTER_NUM_CTX, 'num_predict': ROUTER_NUM_PREDICT, 'num_gpu': 0, 'temperature': 0}}
    timeout = httpx.Timeout(ROUTER_TIMEOUT_SECONDS, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(OLLAMA_BASE_URL + '/api/chat', json=payload)
        response.raise_for_status()
        data = response.json()
    parsed = json.loads(data.get('message', {}).get('content', ''))
    if not isinstance(parsed, dict):
        raise ValueError('router result is not object')
    return normalize_router_result(parsed)

def normalize_capability_result(value):
    local_sufficient = value.get('local_sufficient', True)
    if isinstance(local_sufficient, str):
        local_sufficient = local_sufficient.strip().lower() == 'true'
    try:
        confidence = float(value.get('confidence', 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(confidence, 1.0))
    risk = str(value.get('risk', 'medium')).strip().lower()
    if risk not in {'low', 'medium', 'high'}:
        risk = 'medium'
    reason = str(value.get('reason', '')).strip()
    if len(reason) > 240:
        reason = reason[:240]
    return {'local_sufficient': bool(local_sufficient), 'confidence': confidence, 'risk': risk, 'reason': reason}

async def capability_check(info: dict, deterministic: dict, router: dict):
    system_prompt = '\nJesteś lokalnym audytorem możliwości modelu.\nNIE rozwiązuj zadania użytkownika.\n\nOceń, czy lokalny model Gemma3 klasy około\n12B może udzielić rzetelnej, kompletnej i\nprecyzyjnej odpowiedzi bez pomocy większego\nmodelu.\n\nZwróć WYŁĄCZNIE JSON:\n\n{\n  "local_sufficient": true|false,\n  "confidence": 0.0-1.0,\n  "risk": "low|medium|high",\n  "reason": "krótkie uzasadnienie"\n}\n\nPole confidence oznacza PEWNOŚĆ TWOJEJ OCENY\nlocal_sufficient. NIE oznacza prawdopodobieństwa,\nże model lokalny sobie poradzi.\n\nPrzykład: jeżeli jesteś prawie pewien, że model\nlokalny NIE wystarczy, zwróć:\nlocal_sufficient=false, confidence=0.9.\n\nlocal_sufficient=false wybierz szczególnie,\ngdy zadanie wymaga rygorystycznego dowodu,\nformalnego projektowania, zaawansowanej\nwiedzy domenowej połączonej z wieloma\nograniczeniami, subtelnych przypadków\nbrzegowych albo niezawodnej wieloetapowej\nanalizy, gdzie błąd 12B jest realnym ryzykiem.\n\nJeżeli local_sufficient=false i ryzyko błędu jest\npoważne, ustaw risk=high.\n\nNie eskaluj tylko z powodu prywatności/PII.\nNie oceniaj długości samej w sobie, jeśli\nmieści się w kontekście. Nie wykonuj zadania.\n'.strip()
    sample = str(info.get('sample', ''))
    if len(sample) > CAPABILITY_SAMPLE_CHARS:
        sample = sample[-CAPABILITY_SAMPLE_CHARS:]
    audit_input = {'deterministic_5d': deterministic, 'pre_router': router, 'task_sample': sample}
    payload = {'model': PRIMARY_MODEL, 'messages': [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': json.dumps(audit_input, ensure_ascii=False)}], 'format': 'json', 'stream': False, 'options': {'num_predict': CAPABILITY_NUM_PREDICT, 'temperature': 0}}
    timeout = httpx.Timeout(CAPABILITY_TIMEOUT_SECONDS, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(OLLAMA_BASE_URL + '/api/chat', json=payload)
        response.raise_for_status()
        data = response.json()
    parsed = json.loads(data.get('message', {}).get('content', ''))
    if not isinstance(parsed, dict):
        raise ValueError('capability result is not object')
    return normalize_capability_result(parsed)

async def decide_route(payload: dict):
    messages = payload.get('messages', [])
    info = inspect_messages(messages)
    base = {**info, 'deterministic': None, 'capability': None}
    if cloud_control_command(messages):
        return {'route': 'cloud', 'reason': 'cloud_control', 'router': None, **base}
    if info['has_image']:
        return {'route': 'local', 'reason': 'vision', 'router': {'language': 'other', 'task': 'document', 'complexity': 'medium', 'cloud_candidate': False}, **base}
    if CLOUD_MODE == 'local-only':
        return {'route': 'local', 'reason': 'cloud_disabled', 'router': None, **base}
    if CLOUD_MODE == 'cloud-only':
        return {'route': 'cloud', 'reason': 'cloud_forced', 'router': None, **base}
    if info['total_chars'] > LOCAL_MAX_INPUT_CHARS:
        return {'route': 'cloud', 'reason': 'local_context_threshold', 'router': None, **base}
    deterministic = deterministic_5d(info)
    router_failed = False
    try:
        classification = await classify_with_router(info)
    except Exception:
        router_failed = True
        classification = {'language': 'other', 'task': 'simple', 'complexity': 'medium', 'cloud_candidate': False}
    qwen_candidate = classification['complexity'] == 'high' or (classification['complexity'] == 'medium' and classification['cloud_candidate'] is True)
    needs_capability = deterministic['capability_candidate'] or qwen_candidate
    if not needs_capability:
        return {'route': 'local', 'reason': 'router_fail_local' if router_failed else 'router_local', 'router': classification, **info, 'deterministic': deterministic, 'capability': None}
    try:
        capability = await capability_check(info, deterministic, classification)
    except Exception:
        return {'route': 'local', 'reason': 'capability_fail_local', 'router': classification, **info, 'deterministic': deterministic, 'capability': None}
    cloud_by_capability = capability['local_sufficient'] is False and (capability['risk'] == 'high' or capability['confidence'] >= 0.55)
    cloud_by_extreme = deterministic['extreme'] is True
    use_cloud = cloud_by_extreme or cloud_by_capability
    if cloud_by_extreme:
        reason = 'deterministic_extreme'
    elif cloud_by_capability:
        reason = 'capability_cloud'
    else:
        reason = 'capability_local'
    return {'route': 'cloud' if use_cloud else 'local', 'reason': reason, 'router': classification, **info, 'deterministic': deterministic, 'capability': capability}

def requested_token_limit(source: dict) -> int:
    requested = source.get('max_tokens')
    if requested is None:
        requested = source.get('max_completion_tokens')
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        requested = LOCAL_DEFAULT_MAX_TOKENS
    return max(1, min(requested, LOCAL_MAX_TOKENS))

def local_payload(payload: dict, max_tokens_override: Optional[int]=None):
    source = copy.deepcopy(payload)
    token_limit = requested_token_limit(source)
    if max_tokens_override is not None:
        try:
            override = int(max_tokens_override)
        except (TypeError, ValueError):
            override = token_limit
        token_limit = max(1, min(token_limit, override))
    result = {'model': PRIMARY_MODEL, 'messages': source.get('messages', []), 'stream': bool(source.get('stream', False)), 'max_tokens': token_limit}
    safe_optional = ('temperature', 'top_p', 'stop', 'seed', 'response_format', 'stream_options')
    for key in safe_optional:
        if key in source:
            result[key] = source[key]
    return result

def cloud_payload(payload: dict, reasoning_effort: Optional[str]=None, minimum_completion_tokens: Optional[int]=None):
    source = copy.deepcopy(payload)
    result = {'model': CLOUD_MODEL, 'messages': source.get('messages', []), 'stream': bool(source.get('stream', False))}
    requested = source.get('max_completion_tokens')
    if requested is None:
        requested = source.get('max_tokens')
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        requested = None
    minimum = None
    if minimum_completion_tokens is not None:
        try:
            minimum = int(minimum_completion_tokens)
        except (TypeError, ValueError):
            minimum = None
    if requested is None:
        requested = minimum
    elif minimum is not None:
        requested = max(requested, minimum)
    if requested is not None:
        result['max_completion_tokens'] = max(1, min(requested, CLOUD_HARD_MAX_TOKENS))
    if reasoning_effort in {'medium', 'high'}:
        result['reasoning_effort'] = reasoning_effort
    return result

def cloud_effort_for_request(decision: dict, guidance_metadata: dict) -> str:
    complexity = str(guidance_metadata.get('complexity', 'medium')).lower()
    if complexity == 'high':
        return 'high'
    capability = decision.get('capability')
    if isinstance(capability, dict) and capability.get('risk') == 'high' and (decision.get('route') == 'cloud'):
        return 'high'
    return 'medium'

async def external_cloud_models() -> list[dict]:
    timeout = httpx.Timeout(8.0, connect=3.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        health = await client.get(PRIVACY_GATEWAY_BASE_URL.removesuffix('/v1') + '/health')
        health.raise_for_status()
        data = health.json()
    routes = data.get('model_routes', {})
    if not isinstance(routes, dict):
        return []
    result = []
    for model_id, route_class in routes.items():
        if route_class != 'cloud':
            continue
        result.append({'id': model_id, 'label': str(EXTERNAL_MODEL_LABELS.get(model_id, model_id))})
    return result

def banner_text(route: str, reason: str, payload: dict) -> str:
    if route == 'cloud' and reason == 'cloud_control':
        action = cloud_control_action(payload.get('messages', []))
        if action == 'cancel':
            return '[LOCAL · Privacy Gateway · cloud_cancel]\n\n'
        return '[CLOUD · OpenAI · approved]\n\n'
    if route == 'cloud':
        return '[CLOUD · OpenAI · ' + reason + ']\n\n'
    return '[LOCAL · Gemma3:12B · ' + reason + ']\n\n'

def completion_message_content(data: dict):
    choices = data.get('choices')
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    message = choice.get('message')
    if not isinstance(message, dict):
        return None
    content = message.get('content')
    if not isinstance(content, str):
        return None
    return (message, content)

def add_banner_to_completion(data: dict, route: str, reason: str, payload: dict):
    target = completion_message_content(data)
    if target is None:
        return
    message, content = target
    if not content:
        return
    if content.startswith(APPROVAL_MARKER):
        return
    if BROKER_BANNER_RE.match(content):
        return
    message['content'] = banner_text(route, reason, payload) + content

def split_sse_event(buffer: bytes):
    lf_pos = buffer.find(b'')
    crlf_pos = buffer.find(b'')
    positions = [(lf_pos, 2), (crlf_pos, 4)]
    positions = [item for item in positions if item[0] >= 0]
    if not positions:
        return None
    pos, delimiter_len = min(positions, key=lambda item: item[0])
    end = pos + delimiter_len
    return (buffer[:end], buffer[end:])

def sse_event_json(event: bytes):
    try:
        text = event.decode('utf-8')
    except UnicodeDecodeError:
        return None
    data_lines = []
    for line in text.splitlines():
        if line.startswith('data:'):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return None
    raw = '\n'.join(data_lines).strip()
    if not raw or raw == '[DONE]':
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data

def sse_event_content(event: bytes):
    data = sse_event_json(event)
    if data is None:
        return None
    choices = data.get('choices')
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    delta = choice.get('delta')
    if not isinstance(delta, dict):
        return None
    content = delta.get('content')
    if not isinstance(content, str):
        return None
    return content

def add_banner_to_sse_event(event: bytes, banner: str) -> bytes:
    try:
        text = event.decode('utf-8')
    except UnicodeDecodeError:
        return event
    line_ending = '\r\n' if '\r\n' in text else '\n'
    event_ending = '\r\n\r\n' if text.endswith('\r\n\r\n') else '\n\n' if text.endswith('\n\n') else ''
    core = text[:-len(event_ending)] if event_ending else text
    lines = core.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith('data:'):
            continue
        raw = line[5:].lstrip()
        if not raw or raw == '[DONE]':
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        choices = data.get('choices')
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        delta = choice.get('delta')
        if not isinstance(delta, dict):
            continue
        content = delta.get('content')
        if not isinstance(content, str):
            continue
        delta['content'] = banner + content
        data['model'] = 'secure-auto'
        lines[index] = 'data: ' + json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        return (line_ending.join(lines) + event_ending).encode('utf-8')
    return event

async def stream_with_banner(response: httpx.Response, route: str, reason: str, payload: dict):
    buffer = b''
    pending = []
    prefix = ''
    first_content_index = None
    decided = False
    banner = banner_text(route, reason, payload)
    async for chunk in response.aiter_raw():
        buffer += chunk
        while True:
            split = split_sse_event(buffer)
            if split is None:
                break
            event, buffer = split
            if decided:
                yield event
                continue
            pending.append(event)
            content = sse_event_content(event)
            if not content:
                continue
            if first_content_index is None:
                first_content_index = len(pending) - 1
            prefix += content
            if APPROVAL_MARKER.startswith(prefix):
                if len(prefix) < len(APPROVAL_MARKER):
                    continue
                decided = True
                for item in pending:
                    yield item
                pending = []
                continue
            if prefix.startswith(APPROVAL_MARKER):
                decided = True
                for item in pending:
                    yield item
                pending = []
                continue
            decided = True
            if first_content_index is not None:
                pending[first_content_index] = add_banner_to_sse_event(pending[first_content_index], banner)
            for item in pending:
                yield item
            pending = []
    if buffer:
        if decided:
            yield buffer
        else:
            pending.append(buffer)
    if not decided:
        if first_content_index is not None and (not prefix.startswith(APPROVAL_MARKER)):
            pending[first_content_index] = add_banner_to_sse_event(pending[first_content_index], banner)
        for item in pending:
            yield item

def proxy_response_headers(response: httpx.Response, route: str, reason: str, diagnostic_headers: Optional[dict]=None):
    headers = {'X-AI-Broker-Route': route, 'X-AI-Broker-Reason': reason}
    if isinstance(diagnostic_headers, dict):
        for key, value in diagnostic_headers.items():
            if not key.startswith('X-AI-Broker-'):
                continue
            headers[key] = str(value)[:1024]
    for key, value in response.headers.items():
        lower = key.lower()
        if lower.startswith('x-privacy-') or lower.startswith('x-cloud-'):
            headers[key] = value
    return headers

async def proxy_request(*, url: str, payload: dict, headers: dict, route: str, reason: str, diagnostic_headers: Optional[dict]=None):
    stream = bool(payload.get('stream', False))
    timeout = httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS, connect=15.0)
    client = httpx.AsyncClient(timeout=timeout)
    try:
        request = client.build_request('POST', url, json=payload, headers=headers)
        response = await client.send(request, stream=stream)
    except Exception:
        await client.aclose()
        raise
    broker_headers = proxy_response_headers(response, route, reason, diagnostic_headers)
    content_type = response.headers.get('content-type', 'application/json')
    if not stream:
        try:
            body = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        if response.status_code < 400 and 'application/json' in content_type:
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    data['model'] = 'secure-auto'
                    add_banner_to_completion(data, route, reason, payload)
                    body = json.dumps(data, ensure_ascii=False).encode()
            except Exception:
                pass
        return Response(content=body, status_code=response.status_code, media_type=content_type.split(';', 1)[0], headers=broker_headers)
    if response.status_code >= 400:
        try:
            body = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        return Response(content=body, status_code=response.status_code, media_type=content_type.split(';', 1)[0], headers=broker_headers)

    async def stream_body():
        try:
            async for chunk in stream_with_banner(response, route, reason, payload):
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()
    return StreamingResponse(stream_body(), status_code=response.status_code, media_type='text/event-stream', headers={**broker_headers, 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.get('/health')
async def health():
    ollama_ok = False
    privacy_gateway_ok = False
    timeout = httpx.Timeout(5.0, connect=3.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(OLLAMA_BASE_URL + '/api/tags')
            ollama_ok = response.status_code == 200
        except Exception:
            pass
        try:
            response = await client.get(PRIVACY_GATEWAY_BASE_URL + '/models', headers={'Authorization': 'Bearer ' + PRIVACY_GATEWAY_API_KEY})
            privacy_gateway_ok = response.status_code == 200
        except Exception:
            pass
    status = 'ok' if ollama_ok and privacy_gateway_ok else 'degraded'
    return JSONResponse(status_code=200 if status == 'ok' else 503, content={'status': status, 'version': VERSION, 'ollama': ollama_ok, 'privacy_gateway': privacy_gateway_ok, 'primary_model': PRIMARY_MODEL, 'router_model': ROUTER_MODEL, 'cloud_mode': CLOUD_MODE, 'learning': learning_system.summary()})

@app.get('/v1/models', dependencies=[Depends(require_broker_key)])
async def models():
    return {'object': 'list', 'data': [{'id': 'secure-auto', 'object': 'model', 'created': int(time.time()), 'owned_by': 'ai-broker'}]}

@app.get('/v1/models/{model_id}', dependencies=[Depends(require_broker_key)])
async def model(model_id: str):
    if model_id != 'secure-auto':
        return JSONResponse(status_code=404, content={'error': {'message': 'Model not found', 'type': 'invalid_request_error'}})
    return {'id': 'secure-auto', 'object': 'model', 'created': int(time.time()), 'owned_by': 'ai-broker'}

@app.post('/v1/router/preview', dependencies=[Depends(require_broker_key)])
async def router_preview(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON')
    payload = learning_system.strip_control_history(payload)
    payload = strip_broker_banners(payload)
    decision = await decide_route(payload)
    guidance = learning_system.select_guidance(payload, decision)
    return {'route': decision['route'], 'reason': decision['reason'], 'router': decision['router'], 'deterministic': decision.get('deterministic'), 'capability': decision.get('capability'), 'message_count': decision['message_count'], 'total_chars': decision['total_chars'], 'has_image': decision['has_image'], 'guidance': {'task': guidance['task'], 'complexity': guidance['complexity'], 'scopes': guidance['scopes'], 'guideline_ids': guidance['guideline_ids'], 'trivial': bool(guidance.get('trivial')), 'concise': bool(guidance.get('concise')), 'explanation_requested': bool(guidance.get('explanation_requested')), 'source_guard': bool(guidance.get('source_guard')), 'source_guard_mode': guidance.get('source_guard_mode', 'off'), 'source_messages_demoted': int(guidance.get('source_messages_demoted', 0) or 0), 'source_instructions_quarantined': int(guidance.get('source_instructions_quarantined', 0) or 0), 'source_rag_queries_split': int(guidance.get('source_rag_queries_split', 0) or 0)}}

@app.get('/v1/external/models', dependencies=[Depends(require_broker_key)])
async def external_models():
    try:
        models = await external_cloud_models()
    except Exception:
        return JSONResponse(status_code=503, content={'error': {'message': 'External model registry unavailable', 'type': 'upstream_error'}})
    return {'object': 'list', 'data': models}

@app.post('/v1/external/verify', dependencies=[Depends(require_broker_key)])
async def external_verify(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON body', 'type': 'invalid_request_error'}})
    session_id = session_id_from_request(request)
    if not session_id:
        return JSONResponse(status_code=400, content={'error': {'message': 'External verification requires X-Privacy-Session-ID', 'type': 'invalid_request_error', 'code': '<OPAQUE_VALUE_REDACTED>'}})
    model = str(body.get('model') or '').strip()
    try:
        models = await external_cloud_models()
    except Exception:
        models = []
    allowed = {item['id']: item for item in models if isinstance(item, dict) and item.get('id')}
    if model not in allowed:
        return JSONResponse(status_code=400, content={'error': {'message': 'Selected model is not an enabled cloud model', 'type': 'invalid_request_error', 'code': 'external_model_not_allowed'}})
    requested_effort = str(body.get('reasoning_effort') or 'auto').strip().lower()
    if requested_effort not in VALID_REASONING_EFFORTS:
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid reasoning_effort', 'type': 'invalid_request_error'}})
    mode = str(body.get('mode') or 'critical').strip().lower()
    if mode not in VALID_VERIFY_MODES:
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid verification mode', 'type': 'invalid_request_error'}})
    approval_code = str(body.get('approval_code') or '').strip().upper()
    cancel_approval = bool(body.get('cancel_approval', False))
    source_user_text = str(body.get('source_user_text') or '').strip()
    assistant_text = str(body.get('assistant_text') or '').strip()
    context = None
    if cancel_approval:
        gateway_payload = {'model': model, 'messages': [{'role': 'user', 'content': 'ANULUJ'}], 'stream': False}
        effort = requested_effort if requested_effort in {'medium', 'high'} else 'medium'
    elif approval_code:
        if not re.fullmatch('[A-Z0-9]{8}', approval_code):
            return JSONResponse(status_code=400, content={'error': {'message': 'Invalid approval code', 'type': 'invalid_request_error'}})
        gateway_payload = {'model': model, 'messages': [{'role': 'user', 'content': f'KONTYNUUJ {approval_code}'}], 'stream': False}
        effort = requested_effort if requested_effort in {'medium', 'high'} else 'medium'
    else:
        if not source_user_text or not assistant_text:
            return JSONResponse(status_code=400, content={'error': {'message': 'Missing source_user_text or assistant_text', 'type': 'invalid_request_error'}})
        context = verification_cache.find(session_id, source_user_text)
        if context is None:
            return JSONResponse(status_code=409, content={'error': {'message': 'Original model request context is no longer available. Generate the local answer again and retry verification.', 'type': 'verification_context_error', 'code': '<OPAQUE_VALUE_REDACTED>'}})
        effort = choose_verification_effort(requested_effort, context)
        gateway_payload = build_verification_payload(context, assistant_text, model, mode, effort)
    headers = {'Authorization': 'Bearer ' + PRIVACY_GATEWAY_API_KEY, 'Content-Type': 'application/json', 'X-Privacy-Session-ID': session_id}
    timeout = httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS, connect=15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(PRIVACY_GATEWAY_BASE_URL + '/chat/completions', headers=headers, json=gateway_payload)
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={'error': {'message': 'External verification timeout', 'type': 'upstream_error'}})
    except Exception:
        return JSONResponse(status_code=502, content={'error': {'message': 'External verification unavailable', 'type': 'upstream_error'}})
    passthrough_headers = {}
    for key, value in response.headers.items():
        lower = key.lower()
        if lower.startswith('x-privacy-') or lower.startswith('x-cloud-'):
            passthrough_headers[key] = value
    passthrough_headers['X-AI-Broker-Verification'] = 'true'
    passthrough_headers['<OPAQUE_VALUE_REDACTED>'] = model
    passthrough_headers['<OPAQUE_VALUE_REDACTED>'] = effort
    raw = response.content
    content_type = response.headers.get('content-type', 'application/json')
    if response.status_code < 400 and 'application/json' in content_type:
        try:
            data = response.json()
            target = completion_message_content(data)
            if target is not None:
                message, content = target
                if content and (not content.startswith(APPROVAL_MARKER)):
                    label = allowed[model].get('label', model)
                    banner = '[VERIFICATION · ' + str(label) + ' · ' + effort + ']\n\n'
                    message['content'] = banner + content
                    raw = json.dumps(data, ensure_ascii=False).encode()
        except Exception:
            pass
    return Response(content=raw, status_code=response.status_code, media_type=content_type.split(';', 1)[0], headers=passthrough_headers)

@app.post('/v1/chat/completions', dependencies=[Depends(require_broker_key)])
async def chat_completions(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON body', 'type': 'invalid_request_error'}})
    if payload.get('model') != 'secure-auto':
        return JSONResponse(status_code=400, content={'error': {'message': 'Unsupported model', 'type': 'invalid_request_error', 'code': 'model_not_found'}})
    learning_control = learning_system.process_control(payload)
    if learning_control is not None:
        return local_control_response(payload, learning_control)
    payload = learning_system.strip_control_history(payload)
    payload = strip_broker_banners(payload)
    decision = await decide_route(payload)
    guided_payload, guidance_metadata = learning_system.inject_guidance(payload, decision)
    guidance_ids = guidance_metadata.get('guideline_ids', [])
    diagnostic_headers = {'X-AI-Broker-Task': guidance_metadata.get('task', 'simple'), 'X-AI-Broker-Complexity': guidance_metadata.get('complexity', 'low'), 'X-AI-Broker-Guidance': ','.join(guidance_ids) if guidance_ids else 'none', 'X-AI-Broker-Trivial': 'true' if guidance_metadata.get('trivial') else 'false', 'X-AI-Broker-Concise': 'true' if guidance_metadata.get('concise') else 'false', 'X-AI-Broker-Source-Guard': 'true' if guidance_metadata.get('source_guard') else 'false', 'X-AI-Broker-Source-Guard-Mode': guidance_metadata.get('source_guard_mode', 'off'), 'X-AI-Broker-Source-Demoted': str(int(guidance_metadata.get('source_messages_demoted', 0) or 0)), 'X-AI-Broker-Source-Quarantined': str(int(guidance_metadata.get('source_instructions_quarantined', 0) or 0)), 'X-AI-Broker-RAG-Query-Split': str(int(guidance_metadata.get('source_rag_queries_split', 0) or 0))}
    route = decision['route']
    reason = decision['reason']
    session_id = session_id_from_request(request)
    if session_id:
        verification_cache.remember(session_id, payload, decision, guidance_metadata)
    if route == 'local':
        upstream_url = OLLAMA_BASE_URL + '/v1/chat/completions'
        upstream_payload = local_payload(guided_payload, max_tokens_override=LOCAL_CONCISE_MAX_TOKENS if guidance_metadata.get('concise') else None)
        upstream_headers = {'Content-Type': 'application/json'}
    else:
        upstream_url = PRIVACY_GATEWAY_BASE_URL + '/chat/completions'
        cloud_effort = cloud_effort_for_request(decision, guidance_metadata)
        cloud_budget = completion_tokens_for_effort(cloud_effort)
        diagnostic_headers['<OPAQUE_VALUE_REDACTED>'] = cloud_effort
        diagnostic_headers['<OPAQUE_VALUE_REDACTED>'] = str(cloud_budget)
        upstream_payload = cloud_payload(guided_payload, reasoning_effort=cloud_effort, minimum_completion_tokens=cloud_budget)
        upstream_headers = {'Authorization': 'Bearer ' + PRIVACY_GATEWAY_API_KEY, 'Content-Type': 'application/json'}
        if session_id:
            upstream_headers['X-Privacy-Session-ID'] = session_id
    try:
        return await proxy_request(url=upstream_url, payload=upstream_payload, headers=upstream_headers, route=route, reason=reason, diagnostic_headers=diagnostic_headers)
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={'error': {'message': 'Upstream timeout', 'type': 'upstream_error'}}, headers={'X-AI-Broker-Route': route, 'X-AI-Broker-Reason': reason, **diagnostic_headers})
    except Exception:
        return JSONResponse(status_code=502, content={'error': {'message': 'Upstream unavailable', 'type': 'upstream_error'}}, headers={'X-AI-Broker-Route': route, 'X-AI-Broker-Reason': reason, **diagnostic_headers})

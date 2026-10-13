# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import asyncio
import hashlib
import json
import os
import unicodedata
from urllib.parse import urlparse
import httpx
from app.detectors.base import Detection
from app.sessions import session_manager
AMBIGUOUS_TYPES = {'LOCATION', 'ORGANIZATION'}
LOCAL_OLLAMA_HOSTS = {'host.docker.internal', 'localhost', '127.0.0.1', '::1', 'ollama'}
SYSTEM_PROMPT = '\nJesteś LOKALNYM klasyfikatorem prywatności.\n\nOceniasz dokładnie JEDNĄ encję typu LOCATION albo ORGANIZATION.\n\nZdanie może być w języku polskim albo angielskim.\n\nNie oceniaj, czy sama nazwa miasta lub firmy jest publiczna.\nOceniaj WYŁĄCZNIE RELACJĘ ujawnioną w zdaniu.\n\nMASK:\ngdy zdanie łączy lokalizację lub organizację z prywatną,\nidentyfikowalną osobą albo ujawnia jej prywatną relację.\n\nPrzykłady MASK:\n- Jan Kowalski mieszka w Krakowie.\n- Anna Nowak mieszka w Gdańsku.\n- Jan Kowalski pracuje w Microsoft.\n- Anna Nowak pracuje w Apple.\n- Jan Kowalski leczy się w szpitalu Medico.\n- Anna Nowak studiuje na Uniwersytecie Warszawskim.\n- John Smith lives in London.\n- John Smith works at Microsoft.\n\nKEEP:\ngdy lokalizacja lub organizacja jest elementem publicznej,\nogólnej informacji i zdanie nie ujawnia prywatnej relacji\nidentyfikowalnej osoby.\n\nPrzykłady KEEP:\n- Konferencja odbędzie się w Krakowie.\n- Konferencja odbędzie się w Gdańsku.\n- Spotkanie branżowe odbędzie się w Warszawie.\n- Targi odbywają się w Poznaniu.\n- Microsoft ogłosił wyniki kwartalne.\n- Apple ogłosił nowy produkt.\n- Google otworzył nowe biuro w Warszawie.\n- The conference will take place in London.\n- Microsoft announced quarterly results.\n\nBARDZO WAŻNE:\n- miejsce konferencji, targów lub publicznego wydarzenia -> KEEP\n- miejsce zamieszkania konkretnej osoby -> MASK\n- pracodawca konkretnej osoby -> MASK\n- publiczna informacja o firmie -> KEEP\n- nie maskuj tylko dlatego, że encja jest LOCATION lub ORGANIZATION\n- jeśli relacja jest rzeczywiście niejednoznaczna -> MASK\n\nZwróć WYŁĄCZNIE jeden z dwóch obiektów JSON:\n\n{"decision":"KEEP"}\n\nalbo:\n\n{"decision":"MASK"}\n\nBez wyjaśnienia.\n'.strip()
POLICY_FINGERPRINT = hashlib.sha256(SYSTEM_PROMPT.encode('utf-8')).hexdigest()

def _validate_local_ollama_url(value: str) -> str:
    value = value.rstrip('/')
    parsed = urlparse(value)
    if parsed.scheme != 'http':
        raise RuntimeError('PRIVACY_JUDGE_OLLAMA_URL must use http')
    hostname = parsed.hostname
    if hostname not in LOCAL_OLLAMA_HOSTS:
        raise RuntimeError(f'Privacy judge must use a local Ollama endpoint. Rejected host: {hostname}')
    if parsed.username or parsed.password:
        raise RuntimeError('Privacy judge URL must not contain credentials')
    if parsed.query or parsed.fragment:
        raise RuntimeError('Privacy judge URL must not contain query or fragment')
    return value

def _normalize_cache_text(value: str) -> str:
    value = unicodedata.normalize('NFC', value)
    return ' '.join(value.split())

def _sentence_context(text: str, detection: Detection) -> str:
    start = detection.start
    end = detection.end
    left = start
    while left > 0:
        char = text[left - 1]
        if char in '.!?\n':
            break
        left -= 1
    right = end
    while right < len(text):
        char = text[right]
        if char in '.!?\n':
            right += 1
            break
        right += 1
    context = text[left:right].strip()
    if context:
        return context
    return text[detection.start:detection.end]

def _cache_key(detection: Detection, sentence: str) -> str:
    material = json.dumps({'policy': POLICY_FINGERPRINT, 'type': detection.entity_type.upper(), 'value': _normalize_cache_text(detection.text), 'sentence': _normalize_cache_text(sentence)}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(material.encode('utf-8')).hexdigest()

def _parse_decision(content: str) -> bool:
    pass
    if not content:
        raise ValueError('Privacy judge returned empty content')
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError('Privacy judge response is not an object')
    if set(parsed.keys()) != {'decision'}:
        raise ValueError('Privacy judge response has unexpected fields')
    decision = parsed.get('decision')
    if not isinstance(decision, str):
        raise ValueError('Privacy judge decision is not a string')
    decision = decision.strip().upper()
    if decision == 'MASK':
        return True
    if decision == 'KEEP':
        return False
    raise ValueError('Privacy judge returned invalid decision')

class ContextPrivacyJudge:

    def __init__(self):
        self.model = os.getenv('PRIVACY_JUDGE_MODEL', 'qwen3:8b')
        self.ollama_url = _validate_local_ollama_url(os.getenv('PRIVACY_JUDGE_OLLAMA_URL', 'http://host.docker.internal:11434'))
        self.timeout_seconds = float(os.getenv('PRIVACY_JUDGE_TIMEOUT_SECONDS', '60'))
        self.max_candidates = max(1, int(os.getenv('PRIVACY_JUDGE_MAX_CANDIDATES', '20')))
        self.num_predict = max(16, int(os.getenv('PRIVACY_JUDGE_NUM_PREDICT', '64')))
        self.concurrency = max(1, int(os.getenv('PRIVACY_JUDGE_CONCURRENCY', '2')))
        self.debug = os.getenv('PRIVACY_JUDGE_DEBUG', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}

    async def _classify_one(self, client: httpx.AsyncClient, semaphore: asyncio.Semaphore, session_id: str, local_index: int, group: dict) -> dict:
        representative = group['representative']
        detection = representative['detection']
        request_body = {'model': self.model, 'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': json.dumps({'type': detection.entity_type, 'value': detection.text, 'sentence': representative['sentence']}, ensure_ascii=False)}], 'stream': False, 'think': False, 'format': 'json', 'options': {'temperature': 0, 'num_predict': self.num_predict}}
        await session_manager.record_judge_call(session_id)
        try:
            async with semaphore:
                response = await client.post(self.ollama_url + '/api/chat', json=request_body)
            response.raise_for_status()
            data = response.json()
            if data.get('done_reason') == 'length':
                raise ValueError('Privacy judge hit generation limit')
            message = data.get('message', {})
            content = message.get('content') or ''
            should_mask = _parse_decision(content)
            return {'local_index': local_index, 'group': group, 'should_mask': should_mask, 'failed': False, 'error': None}
        except Exception as exc:
            await session_manager.record_judge_failure(session_id)
            return {'local_index': local_index, 'group': group, 'should_mask': True, 'failed': True, 'error': type(exc).__name__}

    async def decide(self, session_id: str, text: str, detections: list[Detection]) -> set[int]:
        pass
        ambiguous = [detection for detection in detections if detection.entity_type in AMBIGUOUS_TYPES]
        if not ambiguous:
            return set()
        records = []
        for original_index, detection in enumerate(ambiguous):
            sentence = _sentence_context(text, detection)
            records.append({'original_index': original_index, 'detection': detection, 'sentence': sentence, 'cache_key': _cache_key(detection, sentence)})
        keys = [item['cache_key'] for item in records]
        cached = await session_manager.get_contextual_decisions(session_id, keys)
        mask = set()
        for item in records:
            key = item['cache_key']
            if key not in cached:
                continue
            if cached[key]:
                mask.add(item['original_index'])
        missing_by_key = {}
        for item in records:
            key = item['cache_key']
            if key in cached:
                continue
            if key not in missing_by_key:
                missing_by_key[key] = {'representative': item, 'original_indexes': []}
            missing_by_key[key]['original_indexes'].append(item['original_index'])
        unique_missing = list(missing_by_key.values())
        if not unique_missing:
            if self.debug:
                print('PRIVACY_JUDGE_CACHE_ONLY:', len(records))
                print('PRIVACY_JUDGE_MASK:', sorted(mask))
            return mask
        judged = unique_missing[:self.max_candidates]
        overflow = unique_missing[self.max_candidates:]
        overflow_cache = {}
        for group in overflow:
            representative = group['representative']
            key = representative['cache_key']
            overflow_cache[key] = True
            for original_index in group['original_indexes']:
                mask.add(original_index)
        if overflow_cache:
            await session_manager.set_contextual_decisions(session_id, overflow_cache)
        if not judged:
            return mask
        semaphore = asyncio.Semaphore(self.concurrency)
        limits = httpx.Limits(max_connections=self.concurrency, max_keepalive_connections=self.concurrency)
        async with httpx.AsyncClient(timeout=self.timeout_seconds, limits=limits) as client:
            tasks = [self._classify_one(client, semaphore, session_id, local_index, group) for local_index, group in enumerate(judged)]
            results = await asyncio.gather(*tasks)
        successful_cache = {}
        debug_results = []
        for result in results:
            group = result['group']
            representative = group['representative']
            key = representative['cache_key']
            should_mask = result['should_mask']
            if should_mask:
                for original_index in group['original_indexes']:
                    mask.add(original_index)
            if not result['failed']:
                successful_cache[key] = should_mask
            debug_results.append({'id': result['local_index'], 'decision': 'MASK' if should_mask else 'KEEP', 'status': 'fail_closed' if result['failed'] else 'ok', 'error': result['error']})
        await session_manager.set_contextual_decisions(session_id, successful_cache)
        if self.debug:
            print('PRIVACY_JUDGE_MODEL:', self.model)
            print('PRIVACY_JUDGE_OCCURRENCES:', len(records))
            print('PRIVACY_JUDGE_CACHE_HITS:', sum((1 for key in keys if key in cached)))
            print('PRIVACY_JUDGE_UNIQUE_NEW:', len(unique_missing))
            print('PRIVACY_JUDGE_SENT:', len(judged))
            print('PRIVACY_JUDGE_CONCURRENCY:', self.concurrency)
            print('PRIVACY_JUDGE_RESULTS:', json.dumps(debug_results, ensure_ascii=False))
            print('PRIVACY_JUDGE_MASK:', sorted(mask))
        return mask
context_privacy_judge = ContextPrivacyJudge()

# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import json
import re
import time
from app.sessions import session_manager
PARTIAL_TOKEN_RE = re.compile('\\[\\[[A-Z0-9_]*(?:\\])?$')

class TokenStreamRestorer:
    pass

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.pending = {}

    def _split_safe(self, text: str):
        last_token_start = text.rfind('[[')
        if last_token_start >= 0:
            suffix = text[last_token_start:]
            if PARTIAL_TOKEN_RE.fullmatch(suffix):
                return (text[:last_token_start], suffix)
        if text.endswith('['):
            return (text[:-1], '[')
        return (text, '')

    async def feed(self, key, text: str) -> str:
        combined = self.pending.get(key, '') + text
        safe_text, pending_text = self._split_safe(combined)
        self.pending[key] = pending_text
        if not safe_text:
            return ''
        return await session_manager.restore_text(self.session_id, safe_text)

    async def flush(self):
        result = {}
        for key, text in self.pending.items():
            if not text:
                continue
            result[key] = await session_manager.restore_text(self.session_id, text)
        self.pending.clear()
        return result

def _synthetic_chunk(metadata: dict, index: int, field: str, text: str, public_model_id: str):
    return {'id': metadata.get('id', 'chatcmpl-privacy-gateway'), 'object': metadata.get('object', 'chat.completion.chunk'), 'created': metadata.get('created', int(time.time())), 'model': public_model_id, 'choices': [{'index': index, 'delta': {field: text}, 'finish_reason': None}]}

async def transform_openai_stream(upstream, session_id: str, public_model_id: str):
    restorer = TokenStreamRestorer(session_id)
    metadata = {}
    async for line in upstream.aiter_lines():
        if not line:
            continue
        if not line.startswith('data:'):
            yield (line + '\n\n').encode('utf-8')
            continue
        raw_data = line[5:].strip()
        if raw_data == '[DONE]':
            remaining = await restorer.flush()
            for (choice_index, field), text in remaining.items():
                if not text:
                    continue
                chunk = _synthetic_chunk(metadata, choice_index, field, text, public_model_id)
                yield ('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n').encode('utf-8')
            yield b''
            return
        try:
            data = json.loads(raw_data)
        except Exception:
            yield (line + '\n\n').encode('utf-8')
            continue
        for name in ('id', 'object', 'created'):
            if name in data:
                metadata[name] = data[name]
        data['model'] = public_model_id
        choices = data.get('choices')
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                index = choice.get('index', 0)
                delta = choice.get('delta')
                if not isinstance(delta, dict):
                    continue
                for field in ('content', 'reasoning_content'):
                    value = delta.get(field)
                    if not isinstance(value, str):
                        continue
                    delta[field] = await restorer.feed((index, field), value)
        yield ('data: ' + json.dumps(data, ensure_ascii=False) + '\n\n').encode('utf-8')
    remaining = await restorer.flush()
    for (choice_index, field), text in remaining.items():
        if not text:
            continue
        chunk = _synthetic_chunk(metadata, choice_index, field, text, public_model_id)
        yield ('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n').encode('utf-8')

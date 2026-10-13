# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
pass
import asyncio
import json
import re
import urllib.error
import urllib.request
from pydantic import BaseModel, Field

class Filter:

    class Valves(BaseModel):
        broker_base_url: str = Field(default='http://ai-broker:8090/v1', description='')
        broker_api_key: str = Field(default='', description='', json_schema_extra={'input': {'type': 'password'}})
        timeout_seconds: int = Field(default=900, description='')

    def __init__(self):
        self.valves = self.Valves()
    MARKER = '[LOCAL CLOUD APPROVAL]'
    CODE_RE = re.compile('\\bKONTYNUUJ\\s+([A-Z0-9]{8})\\b', re.IGNORECASE)

    def _approval_candidates(self, value, parent=None, key=None, result=None):
        if result is None:
            result = []
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(self.MARKER) and self.CODE_RE.search(stripped):
                result.append((parent, key, value))
            return result
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                self._approval_candidates(child_value, value, child_key, result)
            return result
        if isinstance(value, list):
            for index, child_value in enumerate(value):
                self._approval_candidates(child_value, value, index, result)
        return result

    def _find_approval_node(self, body: dict):
        candidates = self._approval_candidates(body)
        if not candidates:
            return None
        return candidates[-1]

    def _replace_node(self, parent, key, text: str) -> bool:
        if parent is None:
            return False
        try:
            parent[key] = text
            return True
        except Exception:
            return False

    def _strip_marker(self, text: str) -> str:
        if not isinstance(text, str):
            return ''
        result = text.strip()
        if result.startswith(self.MARKER):
            result = result[len(self.MARKER):].lstrip()
        return result

    def _clean_popup_text(self, text: str, code: str) -> str:
        text = self._strip_marker(text)
        text = re.sub('(?im)^\\s*KONTYNUUJ\\s+' + re.escape(code) + '\\s*[.!]?\\s*$', '', text)
        text = re.sub('(?im)^\\s*ANULUJ\\s*[.!]?\\s*$', '', text)
        text = text.strip()
        if len(text) > 1800:
            text = text[:1800] + '\n\n[…]'
        if not text:
            text = 'To żądanie wymaga użycia modelu chmurowego.'
        return text + '\n\n' + 'Czy zezwolić na wysłanie zanonimizowanego żądania do chmury?'

    def _extract_answer(self, data: dict) -> str:
        try:
            content = data['choices'][0]['message'].get('content', '') or ''
            return self._strip_marker(content)
        except Exception:
            return ''

    def _broker_call_sync(self, command: str, chat_id: str) -> dict:
        api_key = self.valves.broker_api_key.strip()
        if not api_key:
            raise RuntimeError('BROKER_API_KEY not configured')
        if not chat_id:
            raise RuntimeError('chat_id unavailable')
        url = self.valves.broker_base_url.rstrip('/') + '/chat/completions'
        payload = {'model': 'secure-auto', 'messages': [{'role': 'user', 'content': command}], 'stream': False}
        request = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json', 'X-Privacy-Session-ID': chat_id}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=self.valves.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')
            raise RuntimeError('Broker HTTP ' + str(exc.code) + ': ' + detail[:500])
        return json.loads(raw.decode('utf-8'))

    async def _broker_call(self, command: str, chat_id: str):
        return await asyncio.to_thread(self._broker_call_sync, command, chat_id)

    async def _notify(self, emitter, level: str, text: str):
        if emitter is None:
            return
        await emitter({'type': 'notification', 'data': {'type': level, 'content': text}})

    async def _status(self, emitter, text: str, done: bool):
        if emitter is None:
            return
        await emitter({'type': 'status', 'data': {'description': text, 'done': done, 'hidden': False}})

    async def outlet(self, body: dict, __event_call__=None, __event_emitter__=None, __metadata__=None) -> dict:
        found = self._find_approval_node(body)
        if found is None:
            return body
        parent, key, approval_text = found
        match = self.CODE_RE.search(approval_text)
        if not match:
            return body
        code = match.group(1).upper()
        if __event_call__ is None:
            await self._notify(__event_emitter__, 'error', 'Brak __event_call__. Użyj ręcznie KONTYNUUJ/ANULUJ.')
            return body
        decision = await __event_call__({'type': 'confirmation', 'data': {'title': 'Secure Auto — użycie chmury', 'message': self._clean_popup_text(approval_text, code)}})
        metadata = __metadata__ if isinstance(__metadata__, dict) else {}
        chat_id = str(body.get('chat_id') or metadata.get('chat_id') or '').strip()
        if not decision:
            if not chat_id:
                self._replace_node(parent, key, 'Użycie chmury zostało odrzucone.')
                await self._notify(__event_emitter__, 'warning', 'Brak chat_id. Pending approval nie mógł zostać anulowany w Gateway.')
                return body
            try:
                result = await self._broker_call('ANULUJ', chat_id)
                answer = self._extract_answer(result)
                if not answer:
                    answer = 'Użycie chmury zostało anulowane.'
                self._replace_node(parent, key, answer)
                await self._notify(__event_emitter__, 'info', 'Użycie chmury zostało anulowane.')
            except Exception as exc:
                self._replace_node(parent, key, 'Użycie chmury zostało odrzucone.')
                await self._notify(__event_emitter__, 'error', 'Błąd ANULUJ: ' + str(exc)[:250])
            return body
        if not chat_id:
            await self._notify(__event_emitter__, 'error', 'Brak chat_id. Użyj ręcznie: KONTYNUUJ ' + code)
            return body
        await self._status(__event_emitter__, 'Zgoda przyjęta. Przetwarzam żądanie w chmurze…', False)
        try:
            result = await self._broker_call('KONTYNUUJ ' + code, chat_id)
            answer = self._extract_answer(result)
            if not answer:
                raise RuntimeError('empty cloud response')
            if self.MARKER in answer:
                raise RuntimeError('unexpected second approval')
            self._replace_node(parent, key, answer)
            await self._status(__event_emitter__, 'Odpowiedź gotowa.', True)
            await self._notify(__event_emitter__, 'success', 'Odpowiedź chmurowa została odebrana.')
        except Exception as exc:
            await self._status(__event_emitter__, 'Przetwarzanie nie powiodło się.', True)
            await self._notify(__event_emitter__, 'error', 'Błąd KONTYNUUJ: ' + str(exc)[:250])
        return body

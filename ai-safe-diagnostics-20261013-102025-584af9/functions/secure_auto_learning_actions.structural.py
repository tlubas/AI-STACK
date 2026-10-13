# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
pass
import os
import re
from typing import Any, Optional
import httpx
from pydantic import BaseModel, Field
CASE_RE = re.compile('\\bC-(\\d{4,})\\b', re.IGNORECASE)
SESSION_ID_RE = re.compile('^[A-Za-z0-9._:\\-]{1,128}$')
LEARNING_MARKERS = ('learning store', 'zapisano korekt', 'zapisano przypadek', 'status: candidate', 'status: approved', 'status: rejected', 'rodzaj: correction', 'rodzaj: example', 'materiał do uczenia')

class Action:
    actions = [{'id': 'approve', 'name': 'Zatwierdź', 'icon_url': ''}, {'id': 'reject', 'name': 'Odrzuć', 'icon_url': ''}, {'id': 'details', 'name': 'Szczegóły', 'icon_url': ''}]

    class Valves(BaseModel):
        broker_base_url: str = Field(default='http://ai-broker:8090/v1', description='')
        broker_api_key: str = Field(default='', description='', json_schema_extra={'input': {'type': 'password'}})
        timeout_seconds: int = Field(default=30, ge=5, le=120, description='')
        priority: int = Field(default=20, description='')

    def __init__(self):
        self.valves = self.Valves()

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                for key in ('text', 'content', 'output_text'):
                    value = item.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
            return '\n'.join(parts)
        if isinstance(content, dict):
            for key in ('text', 'content', 'output_text'):
                value = content.get(key)
                if isinstance(value, str):
                    return value
        return ''

    @classmethod
    def _output_to_text(cls, output: Any) -> str:
        if not isinstance(output, list):
            return ''
        parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get('type') != 'message':
                continue
            content = item.get('content')
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get('text') or part.get('output_text')
                if isinstance(text, str):
                    parts.append(text)
        return '\n'.join(parts)

    @classmethod
    def _message_to_text(cls, message: dict) -> str:
        if not isinstance(message, dict):
            return ''
        text = cls._content_to_text(message.get('content'))
        if text:
            return text
        return cls._output_to_text(message.get('output'))

    @classmethod
    def _case_id_from_text(cls, text: str) -> Optional[str]:
        if not isinstance(text, str):
            return None
        normalized = text.lower()
        if not any((marker in normalized for marker in LEARNING_MARKERS)):
            return None
        matches = CASE_RE.findall(text)
        if not matches:
            return None
        return f'C-{int(matches[-1]):04d}'

    @staticmethod
    def _ids(body: dict, __chat_id__=None, __message_id__=None, __metadata__=None):
        metadata = __metadata__ if isinstance(__metadata__, dict) else {}
        chat_id = __chat_id__ or body.get('chat_id') or metadata.get('chat_id')
        message_id = __message_id__ or body.get('id') or metadata.get('message_id')
        return (str(chat_id or ''), str(message_id or ''))

    @classmethod
    async def _resolve_clicked_message(cls, body: dict, __chat_id__=None, __message_id__=None, __metadata__=None) -> dict:
        pass
        chat_id, message_id = cls._ids(body, __chat_id__, __message_id__, __metadata__)
        if chat_id and message_id:
            try:
                from open_webui.models.chats import Chats
                message = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
                if isinstance(message, dict):
                    return message
            except Exception:
                pass
        direct = {'id': message_id or body.get('id'), 'content': body.get('content')}
        direct_text = cls._message_to_text(direct)
        if direct_text:
            return direct
        message_obj = body.get('message')
        if isinstance(message_obj, dict):
            return message_obj
        messages = body.get('messages')
        if isinstance(messages, list):
            if message_id:
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    if str(message.get('id') or '') == message_id:
                        return message
            for message in reversed(messages):
                if not isinstance(message, dict):
                    continue
                if message.get('role') == 'assistant':
                    return message
        return {}

    def _broker_key(self) -> str:
        return (self.valves.broker_api_key or '').strip() or os.getenv('BROKER_API_KEY', '').strip()

    async def _broker_command(self, command: str, chat_id: str) -> str:
        api_key = self._broker_key()
        if not api_key:
            raise RuntimeError('Brak BROKER_API_KEY. Ustaw broker_api_key w Valves lub zmienną BROKER_API_KEY w kontenerze Open WebUI.')
        base_url = self.valves.broker_base_url.rstrip('/')
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        if chat_id and SESSION_ID_RE.fullmatch(chat_id):
            headers['X-Privacy-Session-ID'] = chat_id
        payload = {'model': 'secure-auto', 'stream': False, 'messages': [{'role': 'user', 'content': command}]}
        timeout = httpx.Timeout(float(self.valves.timeout_seconds), connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f'{base_url}/chat/completions', headers=headers, json=payload)
        if response.status_code >= 400:
            detail = response.text.strip()
            if len(detail) > 600:
                detail = detail[:600] + '…'
            raise RuntimeError(f'AI Broker HTTP {response.status_code}: {detail or 'brak treści'}')
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError('AI Broker zwrócił odpowiedź nie-JSON.') from exc
        try:
            value = data['choices'][0]['message']['content']
            if isinstance(value, str):
                return value.strip()
        except Exception:
            pass
        error = data.get('error') if isinstance(data, dict) else None
        if isinstance(error, dict):
            message = error.get('message')
            if isinstance(message, str):
                return message.strip()
        return 'Brak czytelnej odpowiedzi AI Broker.'

    @staticmethod
    async def _notify(__event_emitter__, kind: str, content: str):
        if __event_emitter__ is None:
            return
        await __event_emitter__({'type': 'notification', 'data': {'type': kind, 'content': content}})

    @staticmethod
    def _replace_status(text: str, new_status: str) -> str:
        updated = re.sub('(?im)^Status:\\s*(candidate|approved|rejected)\\s*$', f'Status: {new_status}', text, count=1)
        if new_status in {'approved', 'rejected'}:
            updated = re.sub('(?ims)\\n+Aby zatwierdzić materiał do uczenia, napisz:\\s*ZATWIERDŹ PRZYPADEK\\s+C-\\d+\\s*', '\n', updated, count=1)
        return updated.rstrip()

    async def action(self, body: dict, __id__=None, __event_emitter__=None, __event_call__=None, __chat_id__=None, __message_id__=None, __metadata__=None, **kwargs):
        chat_id, message_id = self._ids(body, __chat_id__, __message_id__, __metadata__)
        message = await self._resolve_clicked_message(body, __chat_id__, __message_id__, __metadata__)
        text = self._message_to_text(message)
        case_id = self._case_id_from_text(text)
        if not case_id:
            await self._notify(__event_emitter__, 'warning', 'Kliknięta wiadomość nie jest przypadkiem Learning Store (nie znaleziono C-xxxx).')
            return None
        try:
            if __id__ == 'details':
                details = await self._broker_command(f'POKAŻ PRZYPADEK {case_id}', chat_id)
                if __event_call__ is not None:
                    result = await __event_call__({'type': 'confirmation', 'data': {'title': f'Szczegóły {case_id}', 'message': details, 'confirm': 'OK', 'cancel': 'Zamknij'}})
                    if not (isinstance(result, dict) and result.get('error')):
                        return None
                await self._notify(__event_emitter__, 'info', details)
                return None
            if __id__ == 'approve':
                await self._broker_command(f'ZATWIERDŹ PRZYPADEK {case_id}', chat_id)
                await self._notify(__event_emitter__, 'success', f'{case_id} zatwierdzony.')
                if message_id and text:
                    return {'messages': [{'id': message_id, 'content': self._replace_status(text, 'approved')}]}
                return None
            if __id__ == 'reject':
                if __event_call__ is not None:
                    confirmed = await __event_call__({'type': 'confirmation', 'data': {'title': f'Odrzucić {case_id}?', 'message': 'Przypadek pozostanie w Learning Store ze statusem rejected.', 'confirm': 'Odrzuć', 'cancel': 'Anuluj'}})
                    if isinstance(confirmed, dict) and confirmed.get('error'):
                        await self._notify(__event_emitter__, 'error', str(confirmed['error']))
                        return None
                    if not confirmed:
                        return None
                await self._broker_command(f'ODRZUĆ PRZYPADEK {case_id}', chat_id)
                await self._notify(__event_emitter__, 'success', f'{case_id} odrzucony.')
                if message_id and text:
                    return {'messages': [{'id': message_id, 'content': self._replace_status(text, 'rejected')}]}
                return None
            await self._notify(__event_emitter__, 'error', f'Nieznana akcja: {__id__}')
            return None
        except Exception as exc:
            await self._notify(__event_emitter__, 'error', f'Learning Store: {exc}')
            return None

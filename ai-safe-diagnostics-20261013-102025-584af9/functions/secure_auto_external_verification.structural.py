# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
pass
import asyncio
import copy
import logging
import os
import re
import time
from typing import Any, Optional
import httpx
from pydantic import BaseModel, Field
log = logging.getLogger(__name__)
APPROVAL_MARKER = '[LOCAL CLOUD APPROVAL]'
VERIFICATION_TITLE = '### Weryfikacja zewnętrzna'

class ConcurrentMessageChange(RuntimeError):
    pass
APPROVAL_CODE_RE = re.compile('KONTYNUUJ\\s+([A-Z0-9]{8})', re.IGNORECASE)
SESSION_ID_RE = re.compile('^[A-Za-z0-9._:\\-]{1,128}$')

class Action:
    actions = [{'id': 'verify', 'name': 'Wyślij do analizy w modelu zewnętrznym', 'icon_url': ''}]

    class Valves(BaseModel):
        progress_ui_mode: str = Field(default='inline', description='')
        broker_base_url: str = Field(default='http://ai-broker:8090/v1', description='')
        broker_api_key: str = Field(default='', description='', json_schema_extra={'input': {'type': 'password'}})
        timeout_seconds: int = Field(default=900, ge=30, le=1800, description='')
        priority: int = Field(default=30, description='')
        progress_interval_seconds: int = Field(default=10, ge=5, le=120, description='')

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
                value = part.get('text') or part.get('output_text')
                if isinstance(value, str):
                    parts.append(value)
        return '\n'.join(parts)

    @classmethod
    def _message_to_text(cls, message: dict) -> str:
        if not isinstance(message, dict):
            return ''
        text = cls._content_to_text(message.get('content'))
        if text:
            return text
        return cls._output_to_text(message.get('output'))

    @staticmethod
    def _ids(body: dict, __chat_id__=None, __message_id__=None, __metadata__=None):
        metadata = __metadata__ if isinstance(__metadata__, dict) else {}
        chat_id = __chat_id__ or body.get('chat_id') or body.get('session_id') or metadata.get('chat_id')
        message_id = __message_id__ or body.get('id') or metadata.get('message_id')
        return (str(chat_id or ''), str(message_id or ''))

    @classmethod
    async def _resolve_clicked_message(cls, body: dict, __chat_id__=None, __message_id__=None, __metadata__=None) -> dict:
        chat_id, message_id = cls._ids(body, __chat_id__, __message_id__, __metadata__)
        if chat_id and message_id:
            try:
                from open_webui.models.chats import Chats
                message = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
                if isinstance(message, dict):
                    return message
            except Exception:
                pass
        direct = {'id': message_id or body.get('id'), 'content': body.get('content'), 'output': body.get('output'), 'parentId': body.get('parentId') or body.get('parent_id')}
        if cls._message_to_text(direct):
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
                if isinstance(message, dict) and message.get('role') == 'assistant':
                    return message
        return {}

    @classmethod
    async def _source_user_text(cls, body: dict, chat_id: str, message_id: str, clicked: dict) -> str:
        pass
        messages = body.get('messages')
        if isinstance(messages, list):
            clicked_index = None
            if message_id:
                for index, message in enumerate(messages):
                    if not isinstance(message, dict):
                        continue
                    if str(message.get('id') or '') == message_id:
                        clicked_index = index
                        break
            search = messages[:clicked_index] if clicked_index is not None else messages
            for message in reversed(search):
                if not isinstance(message, dict):
                    continue
                if message.get('role') != 'user':
                    continue
                value = cls._message_to_text(message).strip()
                if value:
                    return value
        parent_id = clicked.get('parentId') or clicked.get('parent_id') if isinstance(clicked, dict) else None
        if not (chat_id and parent_id):
            return ''
        try:
            from open_webui.models.chats import Chats
            current_id = str(parent_id)
            for _ in range(12):
                if not current_id:
                    break
                message = await Chats.get_message_by_id_and_message_id(chat_id, current_id)
                if not isinstance(message, dict):
                    break
                value = cls._message_to_text(message).strip()
                if message.get('role') == 'user' and value:
                    return value
                current_id = str(message.get('parentId') or message.get('parent_id') or '')
        except Exception:
            pass
        return ''

    def _broker_key(self) -> str:
        return (self.valves.broker_api_key or '').strip() or os.getenv('BROKER_API_KEY', '').strip()

    async def _request(self, method: str, path: str, *, chat_id: str, json_body: Optional[dict]=None) -> tuple[int, dict]:
        key = self._broker_key()
        if not key:
            raise RuntimeError('Brak BROKER_API_KEY w Valves lub środowisku Open WebUI.')
        headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
        if chat_id and SESSION_ID_RE.fullmatch(chat_id):
            headers['X-Privacy-Session-ID'] = chat_id
        timeout = httpx.Timeout(float(self.valves.timeout_seconds), connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, self.valves.broker_base_url.rstrip('/') + path, headers=headers, json=json_body)
        try:
            data = response.json()
        except Exception:
            data = {'error': {'message': response.text[:800]}}
        return (response.status_code, data)

    @staticmethod
    def _response_error(data: dict) -> str:
        if not isinstance(data, dict):
            return 'Nieprawidłowy format odpowiedzi AI Broker (oczekiwano JSON object).'
        error = data.get('error')
        if isinstance(error, dict):
            return str(error.get('message') or error.get('detail') or error)
        if isinstance(error, str) and error.strip():
            return error.strip()
        return ''

    @staticmethod
    def _completion_text(data: dict) -> str:
        if not isinstance(data, dict):
            return ''
        try:
            value = data['choices'][0]['message']['content']
            if isinstance(value, str):
                return value.strip()
        except (KeyError, IndexError, TypeError):
            pass
        return ''

    @classmethod
    def _require_ok(cls, http_status: int, data: dict, endpoint: str) -> None:
        error = cls._response_error(data)
        if http_status >= 400 or error:
            raise RuntimeError(f'AI Broker {endpoint}: ' + (error or f'HTTP {http_status}'))

    @staticmethod
    async def _emit(__event_emitter__, event: dict) -> None:
        if __event_emitter__ is None:
            return
        try:
            await __event_emitter__(event)
        except Exception:
            log.exception('Nie udało się przesłać zdarzenia Open WebUI')

    @classmethod
    async def _notify(cls, __event_emitter__, kind: str, content: str) -> None:
        await cls._emit(__event_emitter__, {'type': 'notification', 'data': {'type': kind, 'content': content}})

    async def _status(self, __event_emitter__, description: str, done=False) -> None:
        if self.valves.progress_ui_mode != 'status':
            return
        await self._emit(__event_emitter__, {'type': 'status', 'data': {'description': description, 'done': done, 'hidden': False}})

    async def _inline(self, __event_emitter__, source_text: str, label: str, seconds: int) -> None:
        pass
        if self.valves.progress_ui_mode != 'inline':
            return
        extra = '\n\n---\n\n> ⏳ **Trwa weryfikacja zewnętrzna**  \n' + f'> {label}  \n' + f'> Czas oczekiwania: **{seconds} s**. \n' + '> Analiza nie została jeszcze zakończona.'
        await self._emit(__event_emitter__, {'type': 'chat:message', 'data': {'content': source_text.rstrip() + extra}})

    async def _verify_with_progress(self, *, chat_id: str, json_body: dict, __event_emitter__, label: str, source_text: str) -> tuple[int, dict]:
        pass
        started = time.monotonic()
        stop = asyncio.Event()
        await self._notify(__event_emitter__, 'info', 'Zlecono weryfikację przez Privacy Gateway. Oczekuję na wynik modelu zewnętrznego.')
        await self._status(__event_emitter__, f'{label} — 0 s (trwa)')
        await self._inline(__event_emitter__, source_text, label, 0)

        async def ticker():
            interval = int(self.valves.progress_interval_seconds)
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except asyncio.TimeoutError:
                    seconds = int(time.monotonic() - started)
                    description = f'{label} — {seconds} s (w toku)'
                    if self.valves.progress_ui_mode == 'status':
                        await self._status(__event_emitter__, description)
                    elif self.valves.progress_ui_mode == 'popup':
                        await self._notify(__event_emitter__, 'info', description)
                    else:
                        await self._inline(__event_emitter__, source_text, label, seconds)
        task = asyncio.create_task(ticker()) if __event_emitter__ else None
        try:
            return await self._request('POST', '/external/verify', chat_id=chat_id, json_body=json_body)
        finally:
            stop.set()
            if task is not None:
                await task

    @staticmethod
    def _extended_output(clicked: dict, suffix: str) -> Optional[list]:
        pass
        existing = clicked.get('output') if isinstance(clicked, dict) else None
        if not isinstance(existing, list) or not existing:
            return None
        updated_output = copy.deepcopy(existing)
        for item in reversed(updated_output):
            if not isinstance(item, dict) or item.get('type') != 'message':
                continue
            parts = item.get('content')
            if not isinstance(parts, list):
                continue
            for part in reversed(parts):
                if not isinstance(part, dict):
                    continue
                key = next((k for k in ('text', 'output_text') if isinstance(part.get(k), str)), None)
                if key:
                    part[key] += suffix
                    return updated_output
            parts.append({'type': 'output_text', 'text': suffix})
            return updated_output
        updated_output.append({'type': 'message', 'content': [{'type': 'output_text', 'text': suffix}]})
        return updated_output

    @classmethod
    async def _persist_and_show_result(cls, *, chat_id: str, message_id: str, source_text: str, updated: str, updated_output: Optional[list], __event_emitter__) -> bool:
        pass
        persisted = False
        try:
            from open_webui.models.chats import Chats
            stored = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
            if not isinstance(stored, dict) or stored.get('role') != 'assistant':
                raise RuntimeError('Wskazana wiadomość nie istnieje w Chat Store.')
            if cls._message_to_text(stored).strip() != source_text:
                raise ConcurrentMessageChange('Wiadomość zmieniła się w trakcie analizy. Raport nie został automatycznie nadpisany.')
            patch = {'content': updated}
            if updated_output is not None:
                patch['output'] = updated_output
            await Chats.upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, patch)
            reread = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
            persisted = isinstance(reread, dict) and cls._content_to_text(reread.get('content')) == updated and (updated_output is None or reread.get('output') == updated_output)
            if not persisted:
                log.error('Weryfikacja: brak zgodności po odczycie kontrolnym chat_id=%s message_id=%s', chat_id, message_id)
        except ConcurrentMessageChange:
            raise
        except Exception:
            log.exception('Weryfikacja: nieudany zapis chat_id=%s message_id=%s', chat_id, message_id)
        await cls._emit(__event_emitter__, {'type': 'chat:message', 'data': {'content': updated}})
        return persisted

    @staticmethod
    async def _select(__event_call__, *, title: str, message: str, options: list[str], value: Optional[str]=None):
        if __event_call__ is None:
            return False
        result = await __event_call__({'type': 'input', 'data': {'title': title, 'message': message, 'value': value or (options[0] if options else ''), 'input': {'type': 'select', 'options': options}}})
        if isinstance(result, dict) and result.get('error'):
            raise RuntimeError(str(result['error']))
        return result

    async def action(self, body: dict, __id__=None, __event_emitter__=None, __event_call__=None, __chat_id__=None, __message_id__=None, __metadata__=None, **kwargs):
        if __id__ not in {None, 'verify'}:
            return None
        if self.valves.progress_ui_mode not in {'popup', 'status', 'inline'}:
            await self._notify(__event_emitter__, 'error', 'Nieprawidłowy progress_ui_mode: wpisz inline, popup albo status.')
            return None
        chat_id, message_id = self._ids(body, __chat_id__, __message_id__, __metadata__)
        if not chat_id:
            await self._notify(__event_emitter__, 'error', 'Brak CHAT_ID — zewnętrzna weryfikacja wymaga sesji prywatności.')
            return None
        if not message_id:
            await self._notify(__event_emitter__, 'error', 'Brak MESSAGE_ID — nie można bezpiecznie przypisać raportu do wiadomości.')
            return None
        clicked = await self._resolve_clicked_message(body, __chat_id__, __message_id__, __metadata__)
        assistant_text = self._message_to_text(clicked).strip()
        if not assistant_text:
            await self._notify(__event_emitter__, 'error', 'Nie udało się odczytać klikniętej odpowiedzi.')
            return None
        if VERIFICATION_TITLE in assistant_text:
            await self._notify(__event_emitter__, 'warning', 'Ta wiadomość zawiera już raport weryfikacji zewnętrznej.')
            return None
        if assistant_text.startswith(('[CLOUD ·', '[VERIFICATION ·')):
            await self._notify(__event_emitter__, 'info', 'Ta odpowiedź już pochodzi z modelu zewnętrznego.')
            return None
        if assistant_text.startswith(('[LOCAL · Learning Store ·', '[LOCAL · Global Guidance ·')):
            await self._notify(__event_emitter__, 'warning', 'To jest komunikat sterujący, a nie odpowiedź do weryfikacji.')
            return None
        source_user_text = await self._source_user_text(body, chat_id, message_id, clicked)
        if not source_user_text:
            await self._notify(__event_emitter__, 'error', 'Nie udało się znaleźć pytania poprzedzającego tę odpowiedź.')
            return None
        await self._status(__event_emitter__, 'Przygotowanie: pobieram modele zewnętrzne')
        try:
            status, registry = await self._request('GET', '/external/models', chat_id=chat_id)
            self._require_ok(status, registry, '/external/models')
            models = registry.get('data') or []
            if not isinstance(models, list) or not models:
                raise RuntimeError('Broker nie zwrócił listy dozwolonych modeli zewnętrznych.')
            labels = []
            label_to_id = {}
            for item in models:
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get('id') or '').strip()
                label = str(item.get('label') or model_id).strip()
                if not model_id:
                    continue
                display = f'{label} [{model_id}]'
                labels.append(display)
                label_to_id[display] = model_id
            if not labels:
                raise RuntimeError('Lista modeli nie zawiera identyfikatorów.')
            await self._status(__event_emitter__, 'Konfiguracja: wybierz model zewnętrzny')
            selected_model_label = await self._select(__event_call__, title='Model zewnętrzny', message='Wybierz model, który ma zweryfikować lokalną odpowiedź.', options=labels)
            if not selected_model_label:
                await self._status(__event_emitter__, 'Weryfikacja anulowana', True)
                return None
            mode_labels = {'Sprawdź poprawność': 'accuracy', 'Rozwiąż niezależnie i porównaj': 'independent', 'Krytyczna recenzja': 'critical'}
            selected_mode_label = await self._select(__event_call__, title='Tryb weryfikacji', message='Wybierz sposób pracy modelu zewnętrznego.', options=list(mode_labels.keys()), value='Krytyczna recenzja')
            if not selected_mode_label:
                await self._status(__event_emitter__, 'Weryfikacja anulowana', True)
                return None
            effort_labels = {'Auto (Broker wybierze medium/high)': 'auto', 'Medium': 'medium', 'High': 'high'}
            selected_effort_label = await self._select(__event_call__, title='Reasoning', message='High ma większy budżet i może wymagać potwierdzenia kosztu.', options=list(effort_labels.keys()), value='Auto (Broker wybierze medium/high)')
            if not selected_effort_label:
                await self._status(__event_emitter__, 'Weryfikacja anulowana', True)
                return None
            if selected_model_label not in label_to_id or selected_mode_label not in mode_labels or selected_effort_label not in effort_labels:
                raise RuntimeError('Wybór zawiera nieznaną wartość.')
            model_id = label_to_id[selected_model_label]
            mode = mode_labels[selected_mode_label]
            effort = effort_labels[selected_effort_label]
            request_body = {'model': model_id, 'mode': mode, 'reasoning_effort': effort, 'source_user_text': source_user_text, 'assistant_text': assistant_text}
            status, data = await self._verify_with_progress(chat_id=chat_id, json_body=request_body, __event_emitter__=__event_emitter__, label=f'Analiza w modelu {model_id}', source_text=assistant_text)
            self._require_ok(status, data, '/external/verify')
            result = self._completion_text(data)
            if result.startswith(APPROVAL_MARKER):
                code_match = APPROVAL_CODE_RE.search(result)
                if not code_match:
                    raise RuntimeError('Approval Gate zwrócił prośbę o zatwierdzenie bez kodu.')
                if __event_call__ is None:
                    raise RuntimeError('Brak obsługi potwierdzeń Approval Gate w UI.')
                await self._status(__event_emitter__, 'Approval Gate: oczekiwanie na zatwierdzenie kosztu')
                await self._inline(__event_emitter__, assistant_text, 'Approval Gate: oczekuję na Twoją decyzję', 0)
                approved = await __event_call__({'type': 'confirmation', 'data': {'title': 'Zatwierdzić analizę zewnętrzną?', 'message': result, 'confirm': 'Zatwierdź', 'cancel': 'Anuluj'}})
                if isinstance(approved, dict) and approved.get('error'):
                    raise RuntimeError(str(approved['error']))
                if not approved:
                    cancel_status, cancel_data = await self._request('POST', '/external/verify', chat_id=chat_id, json_body={'model': model_id, 'mode': mode, 'reasoning_effort': effort, 'cancel_approval': True})
                    self._require_ok(cancel_status, cancel_data, 'anulowanie Approval Gate')
                    await self._status(__event_emitter__, 'Analiza anulowana', True)
                    if self.valves.progress_ui_mode == 'inline':
                        await self._emit(__event_emitter__, {'type': 'chat:message', 'data': {'content': assistant_text}})
                    return None
                status, data = await self._verify_with_progress(chat_id=chat_id, json_body={'model': model_id, 'mode': mode, 'reasoning_effort': effort, 'approval_code': code_match.group(1).upper()}, __event_emitter__=__event_emitter__, label=f'Analiza po zatwierdzeniu w modelu {model_id}', source_text=assistant_text)
                self._require_ok(status, data, '/external/verify (zatwierdzenie)')
                result = self._completion_text(data)
            if not result:
                raise RuntimeError('Model zewnętrzny zwrócił pustą odpowiedź.')
            if result.startswith(APPROVAL_MARKER):
                raise RuntimeError('Approval Gate ponownie zażądał potwierdzenia.')
            await self._status(__event_emitter__, 'Analiza zakończona: zapisuję i weryfikuję raport')
            suffix = '\n\n---\n\n' + VERIFICATION_TITLE + '\n\n' + f'**Model:** `{model_id}`  |  **Tryb:** `{mode}`  |  ' + f'**Reasoning:** `{effort}`\n\n' + result
            updated = assistant_text.rstrip() + suffix
            updated_output = self._extended_output(clicked, suffix)
            persisted = await self._persist_and_show_result(chat_id=chat_id, message_id=message_id, source_text=assistant_text, updated=updated, updated_output=updated_output, __event_emitter__=__event_emitter__)
            response_patch = {'id': message_id, 'content': updated}
            if updated_output is not None:
                response_patch['output'] = updated_output
            if persisted:
                await self._status(__event_emitter__, 'Weryfikacja zakończona — raport zapisany i potwierdzony w Chat Store', True)
                await self._notify(__event_emitter__, 'success', 'Raport weryfikacji zapisany w rozmowie.')
            else:
                await self._status(__event_emitter__, 'Weryfikacja zakończona — zapis w Chat Store niepotwierdzony', True)
                await self._notify(__event_emitter__, 'warning', 'Raport przekazano do UI, ale zapis w Chat Store nie został potwierdzony. Sprawdź log Open WebUI i odśwież rozmowę.')
            return {'messages': [response_patch]}
        except Exception as exc:
            log.exception('Błąd weryfikacji zewnętrznej')
            if self.valves.progress_ui_mode == 'inline' and (not isinstance(exc, ConcurrentMessageChange)):
                await self._emit(__event_emitter__, {'type': 'chat:message', 'data': {'content': assistant_text}})
            await self._status(__event_emitter__, 'Weryfikacja nie powiodła się', True)
            await self._notify(__event_emitter__, 'error', 'Weryfikacja zewnętrzna: ' + str(exc))
            return None

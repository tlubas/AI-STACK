# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import copy
import os
import re
import sqlite3
from app.source_guard import has_privileged_source_message, protect_payload_sources
import time
from pathlib import Path
from threading import RLock
from typing import Optional
VALID_SCOPES = {'global', 'analysis', 'reasoning', 'document', 'coding', 'translation'}
MAX_GUIDELINE_CHARS = 2000
MAX_GUIDANCE_ITEMS = 20
MAX_GUIDANCE_TOTAL_CHARS = 12000
MAX_CASE_TEXT_CHARS = 24000
MAX_CASE_NOTE_CHARS = 4000
MAX_CASE_CORRECTION_CHARS = 24000
MAX_CASE_LESSON_CHARS = 6000
MAX_CASE_DISPLAY_CHARS = 4000
SHOW_GUIDELINES_RE = re.compile('^\\s*(?:pokaż|pokaz|wyświetl|wyswietl)\\s+globalne\\s+wytyczne\\s*[.!]?\\s*$', re.IGNORECASE)
DELETE_GUIDELINE_RE = re.compile('^\\s*usuń\\s+globaln(?:ą|a)\\s+wytyczn(?:ą|a)\\s+G[- ]?(\\d+)\\s*[.!]?\\s*$', re.IGNORECASE)
SHOW_CASES_RE = re.compile('^\\s*(?:pokaż|pokaz|wyświetl|wyswietl)\\s+(?:zapisane\\s+)?(?:przypadki|learning\\s+store)\\s*[.!]?\\s*$', re.IGNORECASE)
DELETE_CASE_RE = re.compile('^\\s*usuń\\s+przypadek\\s+C[- ]?(\\d+)\\s*[.!]?\\s*$', re.IGNORECASE)
SHOW_CASE_RE = re.compile('^\\s*(?:pokaż|pokaz|wyświetl|wyswietl)\\s+przypadek\\s+C[- ]?(\\d+)\\s*[.!]?\\s*$', re.IGNORECASE)
APPROVE_CASE_RE = re.compile('^\\s*(?:zatwierdź|zatwierdz|zaakceptuj)\\s+przypadek\\s+C[- ]?(\\d+)\\s*[.!]?\\s*$', re.IGNORECASE)
REJECT_CASE_RE = re.compile('^\\s*(?:odrzuć|odrzuc)\\s+przypadek\\s+C[- ]?(\\d+)\\s*[.!]?\\s*$', re.IGNORECASE)
CORRECTION_PREFIX_RE = re.compile('^\\s*(?:to\\s+był\\s+błąd|to\\s+byl\\s+blad|to\\s+było\\s+błędne|to\\s+bylo\\s+bledne|to\\s+jest\\s+błędne|to\\s+jest\\s+bledne|to\\s+było\\s+źle|to\\s+bylo\\s+zle)\\s*[:.\\-]?\\s*', re.IGNORECASE)
CORRECTION_MARKER_RE = re.compile('(?:^|\\n|\\s)(?:poprawna\\s+odpowiedź|poprawna\\s+odpowiedz|powinno\\s+być|powinno\\s+byc)\\s*:\\s*', re.IGNORECASE)
LESSON_MARKER_RE = re.compile('(?:^|\\n)\\s*(?:lekcja|wniosek\\s+na\\s+przyszłość|wniosek\\s+na\\s+przyszlosc)\\s*:\\s*', re.IGNORECASE)
SAVE_CASE_RE = re.compile('^\\s*zapamiętaj\\s+ten\\s+przypadek(?:\\s*:\\s*(.*))?\\s*$', re.IGNORECASE | re.DOTALL)
SAVE_LESSON_RE = re.compile('^\\s*zapamiętaj\\s+(?:to\\s+)?jako\\s+lekcj(?:ę|e)(?:\\s*:\\s*(.*))?\\s*$', re.IGNORECASE | re.DOTALL)
FUTURE_RE = re.compile('^\\s*na\\s+przyszłość\\s*[:,\\-]?\\s*(.+)$', re.IGNORECASE | re.DOTALL)
EXPLICIT_SCOPE_RE = re.compile('^\\s*\\[(global|analysis|reasoning|document|coding|translation)\\]\\s*[:\\-]?\\s*(.+)$', re.IGNORECASE | re.DOTALL)
FENCED_TEXT_RE = re.compile('^\\s*```[^\\n`]*\\n(?P<body>.*?)\\n?```\\s*$', re.DOTALL)
INLINE_CODE_RE = re.compile('^\\s*`(?P<body>[^`]+)`\\s*$', re.DOTALL)
EXPLANATION_REQUEST_RE = re.compile('\\b(?:wyjaśn\\w*|wyjasn\\w*|krok\\s+po\\s+kroku|pokaż\\s+(?:obliczen\\w*|tok\\s+rozumowania|kroki)|pokaz\\s+(?:obliczen\\w*|tok\\s+rozumowania|kroki)|uzasadn\\w*|dlaczego|explain\\w*|show\\s+(?:the\\s+)?work|step\\s+by\\s+step|why)\\b', re.IGNORECASE)
ARITHMETIC_PREFIX_RE = re.compile('^\\s*(?:ile\\s+(?:jest|wynosi)|oblicz|policz|calculate|compute|what\\s+is)\\s*[:=]?\\s*', re.IGNORECASE)
ARITHMETIC_BODY_RE = re.compile('^[0-9\\s.,()+\\-*/%^×÷·]+$')
DOCUMENT_HINT_RE = re.compile('\\b(?:dokument\\w*|plik\\w*|pdf\\w*|załącznik\\w*|zalacznik\\w*|umow\\w*|siwz\\w*|specyfikac\\w*|ocr\\w*|źródł\\w*|zrodl\\w*|materiał\\w*|material\\w*|attachment\\w*|document\\w*|file\\w*)\\b', re.IGNORECASE)
SOURCE_MARKER_RE = re.compile('(?:<source|</source>|<context|</context>|retrieved[_ -]?context|knowledge[_ -]?context|source[_ -]?content|document[_ -]?content|file[_ -]?content)', re.IGNORECASE)

class LearningStore:

    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / 'learning.db'
        self.lock = RLock()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=10000')
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA journal_mode=WAL')
        return connection

    def _ensure_column(self, connection, table: str, column: str, definition: str):
        columns = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})').fetchall()}
        if column not in columns:
            connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')

    def _init_db(self):
        with self.lock:
            with self._connect() as connection:
                connection.executescript("\n                    CREATE TABLE IF NOT EXISTS meta (\n                        key TEXT PRIMARY KEY,\n                        value TEXT NOT NULL\n                    );\n\n                    CREATE TABLE IF NOT EXISTS guidelines (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        scope TEXT NOT NULL,\n                        content TEXT NOT NULL,\n                        enabled INTEGER NOT NULL DEFAULT 1,\n                        source TEXT NOT NULL DEFAULT 'chat',\n                        created_at INTEGER NOT NULL,\n                        updated_at INTEGER NOT NULL,\n                        UNIQUE(scope, content)\n                    );\n\n                    CREATE TABLE IF NOT EXISTS learning_cases (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        scope TEXT NOT NULL,\n                        user_input TEXT NOT NULL,\n                        assistant_output TEXT NOT NULL,\n                        note TEXT NOT NULL DEFAULT '',\n                        status TEXT NOT NULL DEFAULT 'candidate',\n                        source TEXT NOT NULL DEFAULT 'chat',\n                        created_at INTEGER NOT NULL,\n                        updated_at INTEGER NOT NULL\n                    );\n\n                    CREATE INDEX IF NOT EXISTS idx_guidelines_enabled_scope\n                    ON guidelines(enabled, scope, id);\n\n                    CREATE INDEX IF NOT EXISTS idx_learning_cases_status_scope\n                    ON learning_cases(status, scope, id);\n                    ")
                self._ensure_column(connection, 'learning_cases', 'kind', "TEXT NOT NULL DEFAULT 'example'")
                self._ensure_column(connection, 'learning_cases', 'corrected_output', "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(connection, 'learning_cases', 'lesson', "TEXT NOT NULL DEFAULT ''")
                connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '2')")

    def add_guideline(self, scope: str, content: str, source: str='chat') -> tuple[int, bool]:
        scope = normalize_scope(scope)
        content = clean_text(content, MAX_GUIDELINE_CHARS)
        if not content:
            raise ValueError('Pusta wytyczna')
        now = int(time.time())
        with self.lock:
            with self._connect() as connection:
                existing = connection.execute('SELECT id FROM guidelines WHERE scope = ? AND content = ?', (scope, content)).fetchone()
                if existing:
                    connection.execute('UPDATE guidelines SET enabled = 1, updated_at = ? WHERE id = ?', (now, existing['id']))
                    return (int(existing['id']), False)
                cursor = connection.execute('\n                    INSERT INTO guidelines(\n                        scope, content, enabled, source, created_at, updated_at\n                    ) VALUES(?, ?, 1, ?, ?, ?)\n                    ', (scope, content, source, now, now))
                return (int(cursor.lastrowid), True)

    def delete_guideline(self, guideline_id: int) -> bool:
        with self.lock:
            with self._connect() as connection:
                cursor = connection.execute('DELETE FROM guidelines WHERE id = ?', (guideline_id,))
                return cursor.rowcount > 0

    def list_guidelines(self, scopes: Optional[list[str]]=None, enabled_only: bool=True, limit: int=200) -> list[dict]:
        conditions = []
        params: list[object] = []
        if enabled_only:
            conditions.append('enabled = 1')
        if scopes:
            normalized = []
            for scope in scopes:
                value = normalize_scope(scope)
                if value not in normalized:
                    normalized.append(value)
            placeholders = ','.join(('?' for _ in normalized))
            conditions.append(f'scope IN ({placeholders})')
            params.extend(normalized)
        where = ''
        if conditions:
            where = ' WHERE ' + ' AND '.join(conditions)
        query = 'SELECT id, scope, content, enabled, source, created_at, updated_at FROM guidelines' + where + ' ORDER BY id ASC LIMIT ?'
        params.append(max(1, min(int(limit), 1000)))
        with self.lock:
            with self._connect() as connection:
                rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def add_case(self, scope: str, user_input: str, assistant_output: str, note: str='', status: str='candidate', source: str='chat', kind: str='example', corrected_output: str='', lesson: str='') -> int:
        scope = normalize_scope(scope)
        user_input = clean_text(user_input, MAX_CASE_TEXT_CHARS)
        assistant_output = clean_text(assistant_output, MAX_CASE_TEXT_CHARS)
        note = clean_text(note, MAX_CASE_NOTE_CHARS)
        corrected_output = clean_text(corrected_output, MAX_CASE_CORRECTION_CHARS)
        lesson = clean_text(lesson, MAX_CASE_LESSON_CHARS)
        if not user_input or not assistant_output:
            raise ValueError('Brak poprzedniej pary pytanie/odpowiedź')
        if status not in {'candidate', 'approved', 'rejected'}:
            status = 'candidate'
        if kind not in {'example', 'correction'}:
            kind = 'example'
        if kind == 'correction' and (not corrected_output):
            raise ValueError('Korekta wymaga poprawnej odpowiedzi')
        now = int(time.time())
        with self.lock:
            with self._connect() as connection:
                cursor = connection.execute('\n                    INSERT INTO learning_cases(\n                        scope, user_input, assistant_output, note,\n                        status, source, created_at, updated_at,\n                        kind, corrected_output, lesson\n                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ', (scope, user_input, assistant_output, note, status, source, now, now, kind, corrected_output, lesson))
                return int(cursor.lastrowid)

    def update_case_status(self, case_id: int, status: str) -> bool:
        if status not in {'candidate', 'approved', 'rejected'}:
            raise ValueError('Nieprawidłowy status przypadku')
        now = int(time.time())
        with self.lock:
            with self._connect() as connection:
                cursor = connection.execute('UPDATE learning_cases SET status = ?, updated_at = ? WHERE id = ?', (status, now, case_id))
                return cursor.rowcount > 0

    def get_case(self, case_id: int) -> Optional[dict]:
        with self.lock:
            with self._connect() as connection:
                row = connection.execute('\n                    SELECT id, scope, user_input, assistant_output, note,\n                           status, source, created_at, updated_at,\n                           kind, corrected_output, lesson\n                    FROM learning_cases\n                    WHERE id = ?\n                    ', (case_id,)).fetchone()
        return dict(row) if row else None

    def delete_case(self, case_id: int) -> bool:
        with self.lock:
            with self._connect() as connection:
                cursor = connection.execute('DELETE FROM learning_cases WHERE id = ?', (case_id,))
                return cursor.rowcount > 0

    def list_cases(self, limit: int=20) -> list[dict]:
        with self.lock:
            with self._connect() as connection:
                rows = connection.execute('\n                    SELECT id, scope, note, status, source, created_at,\n                           kind, lesson\n                    FROM learning_cases\n                    ORDER BY id DESC\n                    LIMIT ?\n                    ', (max(1, min(int(limit), 100)),)).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict:
        with self.lock:
            with self._connect() as connection:
                guidelines = connection.execute('SELECT COUNT(*) AS n FROM guidelines WHERE enabled = 1').fetchone()['n']
                cases = connection.execute('SELECT COUNT(*) AS n FROM learning_cases').fetchone()['n']
                candidates = connection.execute("SELECT COUNT(*) AS n FROM learning_cases WHERE status = 'candidate'").fetchone()['n']
                approved = connection.execute("SELECT COUNT(*) AS n FROM learning_cases WHERE status = 'approved'").fetchone()['n']
                rejected = connection.execute("SELECT COUNT(*) AS n FROM learning_cases WHERE status = 'rejected'").fetchone()['n']
                corrections = connection.execute("SELECT COUNT(*) AS n FROM learning_cases WHERE kind = 'correction'").fetchone()['n']
                schema_row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return {'guidelines': int(guidelines), 'learning_cases': int(cases), 'candidates': int(candidates), 'approved': int(approved), 'rejected': int(rejected), 'corrections': int(corrections), 'schema_version': schema_row['value'] if schema_row else 'unknown', 'db': str(self.db_path)}

def clean_text(value, max_chars: int) -> str:
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if len(value) > max_chars:
        value = value[:max_chars].rstrip()
    return value

def normalize_scope(scope: str) -> str:
    value = str(scope or 'global').strip().lower()
    if value not in VALID_SCOPES:
        return 'global'
    return value

def content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        for key in ('text', 'input_text'):
            value = item.get(key)
            if isinstance(value, str):
                parts.append(value)
    return '\n'.join(parts)

def normalize_user_text(value: str) -> str:
    pass
    text = str(value or '').strip()
    if not text:
        return ''
    fenced = FENCED_TEXT_RE.fullmatch(text)
    if fenced:
        body = fenced.group('body').strip()
        if body:
            return body
    inline = INLINE_CODE_RE.fullmatch(text)
    if inline:
        body = inline.group('body').strip()
        if body:
            return body
    return text

def latest_user_text(payload: dict) -> str:
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return ''
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'user':
            continue
        return normalize_user_text(content_to_text(message.get('content')))
    return ''

def previous_exchange(payload: dict) -> tuple[str, str]:
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return ('', '')
    command_index = None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get('role') == 'user':
            command_index = index
            break
    if command_index is None:
        return ('', '')
    assistant_index = None
    assistant_text = ''
    for index in range(command_index - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'assistant':
            continue
        assistant_text = content_to_text(message.get('content')).strip()
        if assistant_text:
            assistant_index = index
            break
    if assistant_index is None:
        return ('', '')
    user_text = ''
    for index in range(assistant_index - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'user':
            continue
        user_text = content_to_text(message.get('content')).strip()
        if user_text:
            break
    return (user_text, assistant_text)

def explanation_requested(text: str) -> bool:
    value = normalize_user_text(text)
    if not value:
        return False
    return bool(EXPLANATION_REQUEST_RE.search(value))

def is_trivial_query(text: str) -> bool:
    pass
    value = normalize_user_text(text)
    if not value or len(value) > 120:
        return False
    if explanation_requested(value):
        return False
    body = ARITHMETIC_PREFIX_RE.sub('', value, count=1).strip()
    body = body.rstrip('?.! ').strip()
    if not body or len(body) > 80:
        return False
    if not ARITHMETIC_BODY_RE.fullmatch(body):
        return False
    return bool(re.search('[+\\-*/%^×÷·]', body))

def insert_system_messages_before_latest_user(messages: list, system_messages: list[dict]) -> list:
    if not system_messages:
        return copy.deepcopy(messages)
    result = copy.deepcopy(messages)
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if isinstance(message, dict) and message.get('role') == 'user':
            return [*result[:index], *copy.deepcopy(system_messages), *result[index:]]
    return [*result, *copy.deepcopy(system_messages)]

def needs_source_guard(payload: dict, decision: dict, profile: dict) -> bool:
    if profile.get('trivial'):
        return False
    if profile.get('task') == 'document':
        return True
    if bool(decision.get('has_image')):
        return True
    latest = latest_user_text(payload)
    if DOCUMENT_HINT_RE.search(latest or ''):
        return True
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return False
    total_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = content_to_text(message.get('content'))
        total_chars += len(text)
        if SOURCE_MARKER_RE.search(text):
            return True
    return total_chars >= 8000

def parse_correction(text: str) -> Optional[dict]:
    value = normalize_user_text(text)
    if not value:
        return None
    prefix = CORRECTION_PREFIX_RE.match(value)
    if not prefix:
        return None
    remainder = value[prefix.end():]
    marker = CORRECTION_MARKER_RE.search(remainder)
    if not marker:
        return None
    note = remainder[:marker.start()].strip(' \n\t:;,.–—-')
    corrected_and_lesson = remainder[marker.end():].strip()
    if not corrected_and_lesson:
        return None
    lesson = ''
    lesson_marker = LESSON_MARKER_RE.search(corrected_and_lesson)
    if lesson_marker:
        corrected = corrected_and_lesson[:lesson_marker.start()].strip()
        lesson = corrected_and_lesson[lesson_marker.end():].strip()
    else:
        corrected = corrected_and_lesson
    if not corrected:
        return None
    return {'note': clean_text(note, MAX_CASE_NOTE_CHARS), 'corrected_output': clean_text(corrected, MAX_CASE_CORRECTION_CHARS), 'lesson': clean_text(lesson or note, MAX_CASE_LESSON_CHARS)}

def display_text(value: str, max_chars: int=MAX_CASE_DISPLAY_CHARS) -> str:
    text = clean_text(value, max_chars)
    if not text:
        return '—'
    if len(value or '') > len(text):
        return text + '…'
    return text

def infer_scope(text: str) -> str:
    lowered = str(text or '').lower()
    patterns = (('coding', ('\\bkod\\w*', '\\bprogram\\w*', '\\bdebug\\w*', '\\bimplement\\w*', '\\brefaktor\\w*', '\\bpython\\b', '\\bjavascript\\b', '\\btypescript\\b', '\\bsql\\b')), ('document', ('\\bdokument\\w*', '\\bumow\\w*', '\\bsiwz\\b', '\\bspecyfikac\\w*', '\\bzałącznik\\w*', '\\bpdf\\b')), ('translation', ('\\btłumacz\\w*', '\\btlumacz\\w*', '\\bprzekład\\w*', '\\btranslate\\w*')), ('reasoning', ('\\bdow[oó]d\\w*', '\\budowod\\w*', '\\bformal\\w*', '\\blogik\\w*', '\\bwniosk\\w*', '\\brozumow\\w*', '\\bproof\\w*')), ('analysis', ('\\banaliz\\w*', '\\bweryfik\\w*', '\\bzgodn\\w*', '\\bzałoż\\w*', '\\bzaloz\\w*', '\\bkrzyż\\w*', '\\bkrzyz\\w*', '\\bplanowan\\w*', '\\bsprawd\\w*', '\\bporówn\\w*', '\\bporown\\w*')))
    for scope, scope_patterns in patterns:
        if any((re.search(pattern, lowered, re.IGNORECASE) for pattern in scope_patterns)):
            return scope
    return 'global'

def parse_add_guideline(text: str) -> Optional[tuple[str, str]]:
    raw = normalize_user_text(text)
    if not raw:
        return None
    future = FUTURE_RE.fullmatch(raw)
    if future:
        content = future.group(1).strip()
        explicit = EXPLICIT_SCOPE_RE.fullmatch(content)
        if explicit:
            return (normalize_scope(explicit.group(1)), explicit.group(2).strip())
        return (infer_scope(content), content)
    lower = raw.lower()
    if not lower.startswith('dodaj globalnie'):
        return None
    rest = raw[len('dodaj globalnie'):].strip()
    forced_scope = None
    explicit = EXPLICIT_SCOPE_RE.fullmatch(rest)
    if explicit:
        return (normalize_scope(explicit.group(1)), explicit.group(2).strip())
    rest = re.sub('^(?:jako\\s+)?wytyczn(?:ą|a|e|y)\\s*', '', rest, flags=re.IGNORECASE).strip()
    if re.match('^do\\s+przyszłych\\s+analiz', rest, re.IGNORECASE):
        forced_scope = 'analysis'
        rest = re.sub('^do\\s+przyszłych\\s+analiz\\w*\\s*', '', rest, flags=re.IGNORECASE).strip()
    rest = re.sub('^(?:warunki?\\s+kontroln\\w*\\s*)?[:\\-]\\s*', '', rest, flags=re.IGNORECASE).strip()
    if not rest:
        return None
    return (forced_scope or infer_scope(rest), rest)

def decision_profile(payload: dict, decision: dict) -> dict:
    router = decision.get('router')
    task = 'simple'
    complexity = 'low'
    if isinstance(router, dict):
        task = str(router.get('task', 'simple')).lower()
        complexity = str(router.get('complexity', 'low')).lower()
    deterministic = decision.get('deterministic')
    evidence = {}
    if isinstance(deterministic, dict):
        evidence = deterministic.get('evidence') or {}
    latest = latest_user_text(payload)
    wants_explanation = explanation_requested(latest)
    trivial = is_trivial_query(latest)
    if trivial:
        task = 'simple'
        complexity = 'low'
    elif task == 'simple':
        inferred = infer_scope(latest)
        if inferred in {'document', 'coding', 'translation', 'reasoning'}:
            task = inferred
        elif inferred == 'analysis':
            task = 'reasoning'
    if complexity not in {'low', 'medium', 'high'}:
        complexity = 'low'
    if isinstance(deterministic, dict) and (not trivial):
        score = int(deterministic.get('score', 0) or 0)
        if score >= 6:
            complexity = 'high'
        elif score >= 2 and complexity == 'low':
            complexity = 'medium'
    scopes = ['global']
    if task in VALID_SCOPES and task != 'global' and (task != 'simple'):
        scopes.append(task)
    analysis_needed = not trivial and (task in {'reasoning', 'document', 'coding'} or int(evidence.get('reasoning_hits', 0) or 0) > 0 or int(evidence.get('precision_hits', 0) or 0) > 0)
    if analysis_needed and 'analysis' not in scopes:
        scopes.append('analysis')
    reasoning_needed = not trivial and (task == 'reasoning' or complexity == 'high' or int(evidence.get('formal_hits', 0) or 0) > 0)
    if reasoning_needed and 'reasoning' not in scopes:
        scopes.append('reasoning')
    concise = not wants_explanation and (trivial or (task == 'simple' and complexity == 'low'))
    return {'task': task, 'complexity': complexity, 'scopes': scopes, 'trivial': trivial, 'concise': concise, 'explanation_requested': wants_explanation}

class LearningSystem:

    def __init__(self):
        state_dir = os.getenv('BROKER_STATE_DIR', '/broker/state')
        self.store = LearningStore(state_dir)

    def summary(self) -> dict:
        return self.store.summary()

    def is_control_text(self, text: str) -> bool:
        value = normalize_user_text(text)
        if not value:
            return False
        if SHOW_GUIDELINES_RE.fullmatch(value):
            return True
        if DELETE_GUIDELINE_RE.fullmatch(value):
            return True
        if SHOW_CASES_RE.fullmatch(value):
            return True
        if SHOW_CASE_RE.fullmatch(value):
            return True
        if APPROVE_CASE_RE.fullmatch(value):
            return True
        if REJECT_CASE_RE.fullmatch(value):
            return True
        if DELETE_CASE_RE.fullmatch(value):
            return True
        if SAVE_CASE_RE.fullmatch(value):
            return True
        if SAVE_LESSON_RE.fullmatch(value):
            return True
        if parse_correction(value):
            return True
        if parse_add_guideline(value):
            return True
        return False

    def strip_control_history(self, payload: dict) -> dict:
        result = copy.deepcopy(payload)
        messages = result.get('messages')
        if not isinstance(messages, list):
            return result
        cleaned = []
        for message in messages:
            if not isinstance(message, dict):
                cleaned.append(message)
                continue
            role = message.get('role')
            text = normalize_user_text(content_to_text(message.get('content')))
            if role == 'user' and self.is_control_text(text):
                continue
            if role == 'assistant' and (text.startswith('[LOCAL · Global Guidance ·') or text.startswith('[LOCAL · Learning Store ·')):
                continue
            cleaned.append(message)
        result['messages'] = cleaned
        return result

    def process_control(self, payload: dict) -> Optional[str]:
        text = latest_user_text(payload)
        if not text:
            return None
        if SHOW_GUIDELINES_RE.fullmatch(text):
            guidelines = self.store.list_guidelines()
            if not guidelines:
                return '[LOCAL · Global Guidance · list]\n\nBrak aktywnych globalnych wytycznych.\n\nTa komenda została obsłużona lokalnie.'
            lines = ['[LOCAL · Global Guidance · list]', '']
            for item in guidelines:
                lines.append(f'G-{item['id']:04d} [{item['scope']}] {item['content']}')
            lines.extend(['', 'Ta komenda została obsłużona lokalnie.'])
            return '\n'.join(lines)
        match = DELETE_GUIDELINE_RE.fullmatch(text)
        if match:
            guideline_id = int(match.group(1))
            deleted = self.store.delete_guideline(guideline_id)
            if deleted:
                return f'[LOCAL · Global Guidance · deleted]\n\nUsunięto wytyczną G-{guideline_id:04d}.\n\nTa komenda została obsłużona lokalnie.'
            return f'[LOCAL · Global Guidance · not_found]\n\nNie znaleziono wytycznej G-{guideline_id:04d}.'
        parsed = parse_add_guideline(text)
        if parsed:
            scope, content = parsed
            guideline_id, created = self.store.add_guideline(scope=scope, content=content)
            action = 'Dodano' if created else 'Wytyczna już istniała; ponownie ją włączono'
            return f'[LOCAL · Global Guidance · stored]\n\n{action}: G-{guideline_id:04d}\nZakres: {scope}\nTreść: {content}\n\nBędzie stosowana tylko do zapytań pasujących do zakresu. Ta komenda została obsłużona lokalnie i nie została wysłana do modelu.'
        if SHOW_CASES_RE.fullmatch(text):
            cases = self.store.list_cases(limit=20)
            if not cases:
                return '[LOCAL · Learning Store · list]\n\nBrak zapisanych przypadków.\n\nTa komenda została obsłużona lokalnie.'
            lines = ['[LOCAL · Learning Store · list]', '']
            for item in cases:
                note = item.get('note') or 'bez notatki'
                note = note.replace('\n', ' ')
                if len(note) > 160:
                    note = note[:157] + '...'
                kind = item.get('kind') or 'example'
                lines.append(f'C-{item['id']:04d} [{item['scope']}] kind={kind} · status={item['status']} · {note}')
            lines.extend(['', 'Ta komenda została obsłużona lokalnie.'])
            return '\n'.join(lines)
        match = SHOW_CASE_RE.fullmatch(text)
        if match:
            case_id = int(match.group(1))
            item = self.store.get_case(case_id)
            if not item:
                return f'[LOCAL · Learning Store · not_found]\n\nNie znaleziono przypadku C-{case_id:04d}.'
            lines = ['[LOCAL · Learning Store · case]', '', f'C-{case_id:04d}', f'Zakres: {item['scope']}', f'Rodzaj: {item.get('kind') or 'example'}', f'Status: {item['status']}', '', 'Pytanie:', display_text(item.get('user_input', '')), '', 'Poprzednia odpowiedź:', display_text(item.get('assistant_output', ''))]
            if item.get('corrected_output'):
                lines.extend(['', 'Poprawna odpowiedź:', display_text(item['corrected_output'])])
            if item.get('note'):
                lines.extend(['', 'Notatka o błędzie / przypadku:', display_text(item['note'], MAX_CASE_NOTE_CHARS)])
            if item.get('lesson'):
                lines.extend(['', 'Lekcja:', display_text(item['lesson'], MAX_CASE_LESSON_CHARS)])
            lines.extend(['', 'Ta komenda została obsłużona lokalnie.'])
            return '\n'.join(lines)
        match = APPROVE_CASE_RE.fullmatch(text)
        if match:
            case_id = int(match.group(1))
            updated = self.store.update_case_status(case_id, 'approved')
            if not updated:
                return f'[LOCAL · Learning Store · not_found]\n\nNie znaleziono przypadku C-{case_id:04d}.'
            return f'[LOCAL · Learning Store · approved]\n\nZatwierdzono przypadek C-{case_id:04d}.\n\nStatus: approved\nNa tym etapie przypadek jest zatwierdzonym materiałem do uczenia, ale nie jest jeszcze automatycznie wstrzykiwany do odpowiedzi modelu.'
        match = REJECT_CASE_RE.fullmatch(text)
        if match:
            case_id = int(match.group(1))
            updated = self.store.update_case_status(case_id, 'rejected')
            if not updated:
                return f'[LOCAL · Learning Store · not_found]\n\nNie znaleziono przypadku C-{case_id:04d}.'
            return f'[LOCAL · Learning Store · rejected]\n\nOdrzucono przypadek C-{case_id:04d}.\n\nStatus: rejected'
        match = DELETE_CASE_RE.fullmatch(text)
        if match:
            case_id = int(match.group(1))
            deleted = self.store.delete_case(case_id)
            if deleted:
                return f'[LOCAL · Learning Store · deleted]\n\nUsunięto przypadek C-{case_id:04d}.'
            return f'[LOCAL · Learning Store · not_found]\n\nNie znaleziono przypadku C-{case_id:04d}.'
        correction = parse_correction(text)
        if correction:
            user_input, assistant_output = previous_exchange(payload)
            if not user_input or not assistant_output:
                return '[LOCAL · Learning Store · error]\n\nNie znalazłem poprzedniej pary pytanie/odpowiedź do skorygowania.'
            scope = infer_scope('\n'.join((user_input, correction.get('note', ''), correction.get('lesson', ''))))
            case_id = self.store.add_case(scope=scope, user_input=user_input, assistant_output=assistant_output, note=correction.get('note', ''), status='candidate', kind='correction', corrected_output=correction['corrected_output'], lesson=correction.get('lesson', ''))
            return f'[LOCAL · Learning Store · correction]\n\nZapisano korektę jako C-{case_id:04d}.\nZakres: {scope}\nRodzaj: correction\nStatus: candidate'
        match = SAVE_CASE_RE.fullmatch(text) or SAVE_LESSON_RE.fullmatch(text)
        if match:
            note = (match.group(1) or '').strip()
            user_input, assistant_output = previous_exchange(payload)
            if not user_input or not assistant_output:
                return '[LOCAL · Learning Store · error]\n\nNie znalazłem poprzedniej pary pytanie/odpowiedź do zapisania.'
            scope = infer_scope('\n'.join((user_input, note)))
            case_id = self.store.add_case(scope=scope, user_input=user_input, assistant_output=assistant_output, note=note, status='candidate', kind='example', lesson=note)
            return f'[LOCAL · Learning Store · stored]\n\nZapisano przypadek C-{case_id:04d}.\nZakres: {scope}\nRodzaj: example\nStatus: candidate'
        return None

    def select_guidance(self, payload: dict, decision: dict) -> dict:
        profile = decision_profile(payload, decision)
        guidelines = self.store.list_guidelines(scopes=profile['scopes'], enabled_only=True, limit=MAX_GUIDANCE_ITEMS)
        selected = []
        total_chars = 0
        for item in guidelines:
            content = item['content']
            projected = total_chars + len(content)
            if projected > MAX_GUIDANCE_TOTAL_CHARS:
                break
            selected.append(item)
            total_chars = projected
        return {**profile, 'guidelines': selected, 'guideline_ids': [f'G-{item['id']:04d}' for item in selected], 'source_guard': needs_source_guard(payload, decision, profile) or has_privileged_source_message(payload), 'source_guard_mode': 'off', 'source_messages_demoted': 0, 'source_instructions_quarantined': 0, 'source_rag_queries_split': 0}

    def inject_guidance(self, payload: dict, decision: dict) -> tuple[dict, dict]:
        metadata = self.select_guidance(payload, decision)
        guidelines = metadata['guidelines']
        if decision.get('reason') == 'cloud_control':
            return (copy.deepcopy(payload), metadata)
        protected_payload, protection = protect_payload_sources(payload, enabled=bool(metadata.get('source_guard')))
        metadata.update(protection)
        system_messages = []
        if guidelines:
            lines = ['[GLOBAL GUIDANCE]', 'Poniższe reguły są trwałymi wytycznymi użytkownika. ', 'Stosuj je tylko w zakresie pasującym do bieżącego zadania.', 'Nie wspominaj o tej warstwie, chyba że użytkownik o nią zapyta.', '']
            for item in guidelines:
                lines.append(f'- G-{item['id']:04d} [{item['scope']}]: {item['content']}')
            system_messages.append({'role': 'system', 'content': '\n'.join(lines)})
        if metadata.get('concise'):
            system_messages.append({'role': 'system', 'content': '[BROKER RESPONSE MODE: CONCISE]\nTa instrukcja dotyczy wyłącznie bieżącej odpowiedzi. Odpowiedz bezpośrednio i zwięźle. NIE pokazuj procesu rozumowania, kroków pośrednich ani metody rozwiązania, chyba że użytkownik wyraźnie o to poprosił. Dla prostego obliczenia podaj wynik, ewentualnie jedno krótkie zdanie. Nie dodawaj poradnika ani dodatkowej analizy.'})
        if not system_messages:
            return (protected_payload, metadata)
        result = copy.deepcopy(protected_payload)
        messages = result.get('messages')
        if not isinstance(messages, list):
            messages = []
        result['messages'] = insert_system_messages_before_latest_user(messages, system_messages)
        return (result, metadata)
learning_system = LearningSystem()

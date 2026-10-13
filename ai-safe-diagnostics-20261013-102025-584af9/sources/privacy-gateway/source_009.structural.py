# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import asyncio
import copy
import secrets
import string
import time
from dataclasses import dataclass, field
from app.config import settings
TOKEN_ALPHABET = string.ascii_uppercase
APPROVAL_ALPHABET = '<OPAQUE_VALUE_REDACTED>'

@dataclass
class PendingCloudApproval:
    code: str
    created_at: float
    expires_at: float
    public_model: str
    upstream_model: str
    privacy_policy: str
    sanitized_payload: dict
    privacy_stats: dict
    estimate: dict

@dataclass
class PrivacySession:
    session_id: str
    created_at: float
    expires_at: float
    original_to_token: dict = field(default_factory=dict)
    token_to_original: dict = field(default_factory=dict)
    entity_counts: dict = field(default_factory=dict)
    contextual_decision_cache: dict = field(default_factory=dict)
    judge_candidates: int = 0
    judge_cache_hits: int = 0
    judge_cache_misses: int = 0
    judge_calls: int = 0
    judge_fail_closed: int = 0
    pending_cloud_approval: PendingCloudApproval | None = None

class SessionManager:

    def __init__(self):
        self._sessions: dict[str, PrivacySession] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        return time.time()

    def _new_session(self, session_id: str) -> PrivacySession:
        now = self._now()
        return PrivacySession(session_id=session_id, created_at=now, expires_at=now + settings.session_ttl_seconds)

    def _clear_expired_approval_locked(self, session: PrivacySession, now: float | None=None):
        if now is None:
            now = self._now()
        pending = session.pending_cloud_approval
        if pending is not None and pending.expires_at <= now:
            session.pending_cloud_approval = None

    def _get_or_create_locked(self, session_id: str) -> PrivacySession:
        now = self._now()
        session = self._sessions.get(session_id)
        if session and session.expires_at <= now:
            del self._sessions[session_id]
            session = None
        if session is None:
            session = self._new_session(session_id)
            self._sessions[session_id] = session
        self._clear_expired_approval_locked(session, now)
        return session

    def _generate_token(self, session: PrivacySession, entity_type: str) -> str:
        entity_type = entity_type.upper()
        while True:
            random_part = ''.join((secrets.choice(TOKEN_ALPHABET) for _ in range(8)))
            token = f'[[{entity_type}_{random_part}]]'
            if token not in session.token_to_original:
                return token

    def _generate_approval_code(self) -> str:
        return ''.join((secrets.choice(APPROVAL_ALPHABET) for _ in range(8)))

    async def get_or_create(self, session_id: str) -> PrivacySession:
        async with self._lock:
            return self._get_or_create_locked(session_id)

    async def tokenize(self, session_id: str, entity_type: str, original: str) -> str:
        async with self._lock:
            session = self._get_or_create_locked(session_id)
            key = (entity_type.upper(), original)
            existing = session.original_to_token.get(key)
            if existing:
                return existing
            token = self._generate_token(session, entity_type)
            session.original_to_token[key] = token
            session.token_to_original[token] = original
            entity_type = entity_type.upper()
            session.entity_counts[entity_type] = session.entity_counts.get(entity_type, 0) + 1
            return token

    async def get_contextual_decisions(self, session_id: str, keys: list[str]) -> dict[str, bool]:
        pass
        async with self._lock:
            session = self._get_or_create_locked(session_id)
            cache = session.contextual_decision_cache
            found = {key: cache[key] for key in set(keys) if key in cache}
            hits = sum((1 for key in keys if key in cache))
            misses = len(keys) - hits
            session.judge_candidates += len(keys)
            session.judge_cache_hits += hits
            session.judge_cache_misses += misses
            return found

    async def set_contextual_decisions(self, session_id: str, decisions: dict[str, bool]):
        if not decisions:
            return
        async with self._lock:
            session = self._get_or_create_locked(session_id)
            for key, decision in decisions.items():
                session.contextual_decision_cache[key] = bool(decision)

    async def record_judge_call(self, session_id: str):
        async with self._lock:
            session = self._get_or_create_locked(session_id)
            session.judge_calls += 1

    async def record_judge_failure(self, session_id: str):
        async with self._lock:
            session = self._get_or_create_locked(session_id)
            session.judge_fail_closed += 1

    async def create_cloud_approval(self, session_id: str, public_model: str, upstream_model: str, privacy_policy: str, sanitized_payload: dict, privacy_stats: dict, estimate: dict) -> str:
        async with self._lock:
            session = self._get_or_create_locked(session_id)
            now = self._now()
            code = self._generate_approval_code()
            expires_at = min(session.expires_at, now + settings.cloud_approval_ttl_seconds)
            session.pending_cloud_approval = PendingCloudApproval(code=code, created_at=now, expires_at=expires_at, public_model=public_model, upstream_model=upstream_model, privacy_policy=privacy_policy, sanitized_payload=copy.deepcopy(sanitized_payload), privacy_stats=copy.deepcopy(privacy_stats), estimate=copy.deepcopy(estimate))
            return code

    async def consume_cloud_approval(self, session_id: str, public_model: str, code: str):
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return ('not_found', None)
            now = self._now()
            if session.expires_at <= now:
                del self._sessions[session_id]
                return ('expired', None)
            pending = session.pending_cloud_approval
            if pending is None:
                return ('not_found', None)
            if pending.expires_at <= now:
                session.pending_cloud_approval = None
                return ('expired', None)
            if pending.public_model != public_model:
                return ('model_mismatch', None)
            if pending.code != code:
                return ('code_mismatch', None)
            session.pending_cloud_approval = None
            return ('ok', {'public_model': pending.public_model, 'upstream_model': pending.upstream_model, 'privacy_policy': pending.privacy_policy, 'sanitized_payload': copy.deepcopy(pending.sanitized_payload), 'privacy_stats': copy.deepcopy(pending.privacy_stats), 'estimate': copy.deepcopy(pending.estimate)})

    async def cancel_cloud_approval(self, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            now = self._now()
            if session.expires_at <= now:
                del self._sessions[session_id]
                return False
            self._clear_expired_approval_locked(session, now)
            if session.pending_cloud_approval is None:
                return False
            session.pending_cloud_approval = None
            return True

    async def restore_text(self, session_id: str, text: str) -> str:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return text
            if session.expires_at <= self._now():
                del self._sessions[session_id]
                return text
            mapping = dict(session.token_to_original)
        for token, original in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
            text = text.replace(token, original)
        return text

    async def status(self, session_id: str):
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            now = self._now()
            if session.expires_at <= now:
                del self._sessions[session_id]
                return None
            self._clear_expired_approval_locked(session, now)
            pending = session.pending_cloud_approval
            if pending is None:
                approval_status = {'pending': False}
            else:
                approval_status = {'pending': True, 'expires_in_seconds': max(0, int(pending.expires_at - now)), 'estimated_max_cost_usd': pending.estimate.get('estimated_max_cost_usd')}
            return {'session_id': session.session_id, 'created_at': session.created_at, 'expires_at': session.expires_at, 'expires_in_seconds': max(0, int(session.expires_at - now)), 'entities': dict(session.entity_counts), 'unique_tokens': len(session.token_to_original), 'contextual_judge': {'candidates': session.judge_candidates, 'cache_hits': session.judge_cache_hits, 'cache_misses': session.judge_cache_misses, 'calls': session.judge_calls, 'fail_closed': session.judge_fail_closed, 'cache_entries': len(session.contextual_decision_cache)}, 'cloud_approval': approval_status}

    async def destroy(self, session_id: str) -> bool:
        async with self._lock:
            return self._sessions.pop(session_id, None) is not None

    async def purge_expired(self):
        async with self._lock:
            now = self._now()
            expired = [session_id for session_id, session in self._sessions.items() if session.expires_at <= now]
            for session_id in expired:
                del self._sessions[session_id]
            for session in self._sessions.values():
                self._clear_expired_approval_locked(session, now)
            return len(expired)

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(30)
            await self.purge_expired()
session_manager = SessionManager()

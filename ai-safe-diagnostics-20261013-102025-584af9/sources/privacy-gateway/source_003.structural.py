# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import json
import os
VALID_PRIVACY_POLICIES = {'standard', 'strict', 'contextual'}
VALID_ROUTE_CLASSES = {'local', 'cloud'}
DEFAULT_MODEL_REGISTRY = {'secure-fast': {'upstream': 'local-qwen3', 'role': 'fast', 'policy': 'standard', 'route': 'local'}, 'secure-fast-strict': {'upstream': 'local-qwen3', 'role': 'fast', 'policy': 'strict', 'route': 'local'}, 'secure-fast-contextual': {'upstream': 'local-qwen3', 'role': 'fast', 'policy': 'contextual', 'route': 'local'}, 'secure-reasoning': {'upstream': 'local-deepseek', 'role': 'reasoning', 'policy': 'standard', 'route': 'local'}, 'secure-polish': {'upstream': 'local-bielik', 'role': 'polish', 'policy': 'standard', 'route': 'local'}}

def _env_bool(name: str, default: bool=False) -> bool:
    raw = os.getenv(name, 'true' if default else 'false')
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}

def _env_int(name: str, default: int, minimum: int=1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f'{name} must be an integer') from exc
    if value < minimum:
        raise RuntimeError(f'{name} must be >= {minimum}')
    return value

def _env_float(name: str, default: float, minimum_exclusive: float=0.0) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f'{name} must be a number') from exc
    if value <= minimum_exclusive:
        raise RuntimeError(f'{name} must be > {minimum_exclusive}')
    return value

class Settings:

    def __init__(self):
        self.litellm_base_url = os.getenv('LITELLM_BASE_URL', 'http://litellm:4000/v1').rstrip('/')
        self.litellm_api_key = os.getenv('LITELLM_API_KEY', '')
        self.gateway_api_key = os.getenv('GATEWAY_API_KEY', '')
        self.session_ttl_seconds = _env_int('SESSION_TTL_SECONDS', 3600, 60)
        self.cloud_egress_enabled = _env_bool('ENABLE_CLOUD_EGRESS', False)
        self.cloud_approval_enabled = _env_bool('ENABLE_CLOUD_APPROVAL_GATE', True)
        self.cloud_approval_ttl_seconds = _env_int('CLOUD_APPROVAL_TTL_SECONDS', 300, 30)
        self.cloud_soft_max_messages = _env_int('CLOUD_SOFT_MAX_MESSAGES', 32)
        self.cloud_soft_max_input_chars = _env_int('CLOUD_SOFT_MAX_INPUT_CHARS', 60000)
        self.cloud_soft_max_completion_tokens = _env_int('<OPAQUE_VALUE_REDACTED>', 2048)
        self.cloud_soft_max_estimated_usd = _env_float('<OPAQUE_VALUE_REDACTED>', 0.01)
        self.cloud_hard_max_messages = _env_int('CLOUD_HARD_MAX_MESSAGES', 128)
        self.cloud_hard_max_input_chars = _env_int('CLOUD_HARD_MAX_INPUT_CHARS', 500000)
        self.cloud_hard_max_completion_tokens = _env_int('<OPAQUE_VALUE_REDACTED>', 16384)
        self.cloud_default_completion_tokens = _env_int('<OPAQUE_VALUE_REDACTED>', 1024)
        self.cloud_estimate_chars_per_token = _env_float('<OPAQUE_VALUE_REDACTED>', 2.0)
        registry_raw = os.getenv('MODEL_REGISTRY_JSON', '').strip()
        if len(registry_raw) >= 2 and registry_raw[0] == registry_raw[-1] and (registry_raw[0] in {"'", '"'}):
            registry_raw = registry_raw[1:-1]
        if registry_raw:
            try:
                self.model_registry = json.loads(registry_raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f'Invalid MODEL_REGISTRY_JSON: {exc}') from exc
        else:
            self.model_registry = {key: dict(value) for key, value in DEFAULT_MODEL_REGISTRY.items()}
        self._validate()

    def _validate(self):
        if not self.litellm_api_key:
            raise RuntimeError('LITELLM_API_KEY is not configured')
        if not self.gateway_api_key:
            raise RuntimeError('GATEWAY_API_KEY is not configured')
        if self.cloud_soft_max_messages > self.cloud_hard_max_messages:
            raise RuntimeError('CLOUD_SOFT_MAX_MESSAGES cannot exceed CLOUD_HARD_MAX_MESSAGES')
        if self.cloud_soft_max_input_chars > self.cloud_hard_max_input_chars:
            raise RuntimeError('CLOUD_SOFT_MAX_INPUT_CHARS cannot exceed CLOUD_HARD_MAX_INPUT_CHARS')
        if self.cloud_soft_max_completion_tokens > self.cloud_hard_max_completion_tokens:
            raise RuntimeError('CLOUD_SOFT_MAX_COMPLETION_TOKENS cannot exceed CLOUD_HARD_MAX_COMPLETION_TOKENS')
        if self.cloud_default_completion_tokens > self.cloud_hard_max_completion_tokens:
            raise RuntimeError('CLOUD_DEFAULT_COMPLETION_TOKENS cannot exceed CLOUD_HARD_MAX_COMPLETION_TOKENS')
        if not isinstance(self.model_registry, dict):
            raise RuntimeError('MODEL_REGISTRY_JSON must be an object')
        if not self.model_registry:
            raise RuntimeError('Model registry cannot be empty')
        for public_id, config in self.model_registry.items():
            if not isinstance(public_id, str) or not public_id.strip():
                raise RuntimeError('Invalid public model id')
            if not isinstance(config, dict):
                raise RuntimeError(f'Invalid model config: {public_id}')
            upstream = config.get('upstream')
            if not isinstance(upstream, str) or not upstream.strip():
                raise RuntimeError(f'Missing upstream for {public_id}')
            upstream = upstream.strip()
            policy = str(config.get('policy', 'standard')).strip().lower()
            if policy not in VALID_PRIVACY_POLICIES:
                raise RuntimeError(f'Invalid privacy policy for {public_id}: {policy}')
            if 'route' not in config:
                raise RuntimeError(f'Missing explicit route for {public_id}')
            route = str(config['route']).strip().lower()
            if route not in VALID_ROUTE_CLASSES:
                raise RuntimeError(f'Invalid route class for {public_id}: {route}')
            if route == 'local' and (not upstream.startswith('local-')):
                raise RuntimeError(f'Local model {public_id} must use a local-* upstream')
            if route == 'cloud' and upstream.startswith('local-'):
                raise RuntimeError(f'Cloud model {public_id} cannot use a local-* upstream')
            if route == 'cloud' and policy == 'standard':
                raise RuntimeError(f"Cloud model {public_id} cannot use privacy policy 'standard'")
            if route == 'cloud' and (not self.cloud_egress_enabled):
                raise RuntimeError(f'Cloud model {public_id} configured while ENABLE_CLOUD_EGRESS is false')
            if route == 'cloud':
                pricing = config.get('pricing')
                if not isinstance(pricing, dict):
                    raise RuntimeError(f'Cloud model {public_id} requires pricing')
                normalized_pricing = {}
                for key in ('input_usd_per_million', 'output_usd_per_million'):
                    value = pricing.get(key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                        raise RuntimeError(f'Invalid pricing {key} for {public_id}')
                    normalized_pricing[key] = float(value)
                long_threshold = pricing.get('long_context_threshold_tokens')
                if long_threshold is not None:
                    if isinstance(long_threshold, bool) or not isinstance(long_threshold, int) or long_threshold < 1:
                        raise RuntimeError(f'Invalid long-context threshold for {public_id}')
                    for key in ('long_input_usd_per_million', 'long_output_usd_per_million'):
                        value = pricing.get(key)
                        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                            raise RuntimeError(f'Invalid pricing {key} for {public_id}')
                        normalized_pricing[key] = float(value)
                    normalized_pricing['<OPAQUE_VALUE_REDACTED>'] = long_threshold
                config['pricing'] = normalized_pricing
            config['upstream'] = upstream
            config['policy'] = policy
            config['route'] = route

    @property
    def public_models(self):
        return list(self.model_registry.keys())

    def has_model(self, public_id: str):
        return public_id in self.model_registry

    def upstream_for(self, public_id: str):
        return self.model_registry[public_id]['upstream']

    def policy_for(self, public_id: str):
        return self.model_registry[public_id]['policy']

    def route_for(self, public_id: str):
        return self.model_registry[public_id]['route']

    def pricing_for(self, public_id: str):
        return self.model_registry[public_id].get('pricing')

    def metadata_for(self, public_id: str):
        return self.model_registry[public_id]
settings = Settings()

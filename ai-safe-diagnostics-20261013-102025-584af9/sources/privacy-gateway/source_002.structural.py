# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import math
from decimal import Decimal, ROUND_UP
from app.config import settings

class CloudBudgetError(ValueError):
    pass

def _completion_token_limit(payload: dict) -> int:
    has_old = 'max_tokens' in payload
    has_new = 'max_completion_tokens' in payload
    if has_old and has_new:
        raise CloudBudgetError('Specify only one completion token limit')
    if has_new:
        value = payload['max_completion_tokens']
    elif has_old:
        value = payload['max_tokens']
    else:
        value = settings.cloud_default_completion_tokens
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CloudBudgetError('Invalid cloud completion token limit')
    return value

def assess_cloud_request(payload: dict, public_model: str) -> dict:
    messages = payload.get('messages')
    if not isinstance(messages, list):
        raise CloudBudgetError('Cloud request requires a messages list')
    if not messages:
        raise CloudBudgetError('Cloud request has no messages')
    total_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            raise CloudBudgetError('Invalid cloud message')
        content = message.get('content')
        if not isinstance(content, str):
            raise CloudBudgetError('Cloud cost estimation supports text-only content')
        total_chars += len(content)
    message_count = len(messages)
    output_tokens = _completion_token_limit(payload)
    estimated_input_tokens = math.ceil(total_chars / settings.cloud_estimate_chars_per_token) + 8 * message_count + 16
    pricing = settings.pricing_for(public_model)
    if not pricing:
        raise CloudBudgetError('Cloud pricing is not configured')
    input_rate = Decimal(str(pricing['input_usd_per_million']))
    output_rate = Decimal(str(pricing['output_usd_per_million']))
    long_threshold = pricing.get('long_context_threshold_tokens')
    if long_threshold is not None and estimated_input_tokens > long_threshold:
        input_rate = Decimal(str(pricing['long_input_usd_per_million']))
        output_rate = Decimal(str(pricing['long_output_usd_per_million']))
    million = Decimal('1000000')
    estimated_cost = (Decimal(estimated_input_tokens) * input_rate / million + Decimal(output_tokens) * output_rate / million).quantize(Decimal('0.000001'), rounding=ROUND_UP)
    hard_reasons = []
    if message_count > settings.cloud_hard_max_messages:
        hard_reasons.append('liczba wiadomości')
    if total_chars > settings.cloud_hard_max_input_chars:
        hard_reasons.append('rozmiar wejścia')
    if output_tokens > settings.cloud_hard_max_completion_tokens:
        hard_reasons.append('limit odpowiedzi')
    soft_reasons = []
    if message_count > settings.cloud_soft_max_messages:
        soft_reasons.append('liczba wiadomości')
    if total_chars > settings.cloud_soft_max_input_chars:
        soft_reasons.append('rozmiar wejścia')
    if output_tokens > settings.cloud_soft_max_completion_tokens:
        soft_reasons.append('limit odpowiedzi')
    if estimated_cost > Decimal(str(settings.cloud_soft_max_estimated_usd)):
        soft_reasons.append('szacowany koszt')
    return {'message_count': message_count, 'input_chars': total_chars, 'estimated_input_tokens': estimated_input_tokens, 'max_output_tokens': output_tokens, 'estimated_max_cost_usd': float(estimated_cost), 'hard_block': bool(hard_reasons), 'hard_reasons': hard_reasons, 'approval_required': settings.cloud_approval_enabled and bool(soft_reasons), 'soft_reasons': soft_reasons}

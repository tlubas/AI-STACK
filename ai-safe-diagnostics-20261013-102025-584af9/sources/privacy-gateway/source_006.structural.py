# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
from app.config import VALID_PRIVACY_POLICIES
from app.context_judge import AMBIGUOUS_TYPES, context_privacy_judge
from app.detectors.base import Detection
ALWAYS_PSEUDONYMIZE = {'PERSON', 'ADDRESS', 'POSTAL_CODE', 'EMAIL', 'PHONE', 'PESEL', 'NIP', 'REGON', 'ID_CARD', 'IBAN', 'CREDIT_CARD', 'IP_ADDRESS'}
STRICT_ADDITIONAL = {'LOCATION', 'ORGANIZATION'}

def normalize_policy_name(policy_name: str | None) -> str:
    name = policy_name or 'standard'
    name = str(name).strip().lower()
    if name not in VALID_PRIVACY_POLICIES:
        raise ValueError(f'Invalid privacy policy: {name}')
    return name

class PrivacyPolicyEngine:

    async def select(self, session_id: str, detections: list[Detection], text: str, policy_name: str='standard') -> list[Detection]:
        policy_name = normalize_policy_name(policy_name)
        selected = []
        ambiguous = []
        for detection in detections:
            entity_type = detection.entity_type.upper()
            if entity_type in ALWAYS_PSEUDONYMIZE:
                selected.append(detection)
                continue
            if policy_name == 'strict':
                if entity_type in STRICT_ADDITIONAL:
                    selected.append(detection)
                continue
            if policy_name == 'contextual':
                if entity_type in AMBIGUOUS_TYPES:
                    ambiguous.append(detection)
        if policy_name != 'contextual' or not ambiguous:
            return sorted(selected, key=lambda item: (item.start, item.end))
        mask_indexes = await context_privacy_judge.decide(session_id, text, ambiguous)
        for index, detection in enumerate(ambiguous):
            if index in mask_indexes:
                selected.append(detection)
        return sorted(selected, key=lambda item: (item.start, item.end))
policy_engine = PrivacyPolicyEngine()

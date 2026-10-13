# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import re
from datetime import date
import phonenumbers
from app.detectors.base import Detection
PESEL_RE = re.compile('(?<!\\d)\\d{11}(?!\\d)')
NIP_RE = re.compile('(?<!\\d)(?:\\d{10}|\\d{3}[- ]\\d{3}[- ]\\d{2}[- ]\\d{2})(?!\\d)')
REGON_RE = re.compile('(?<!\\d)(?:\\d{14}|\\d{9})(?!\\d)')
ID_CARD_RE = re.compile('(?<![A-Z0-9])[A-Z]{3}[ ]?\\d{6}(?![A-Z0-9])', re.IGNORECASE)
PL_IBAN_RE = re.compile('(?<![A-Z0-9])PL(?:[ ]?\\d){26}(?![A-Z0-9])', re.IGNORECASE)
PL_NRB_RE = re.compile('(?<!\\d)(?:\\d[ ]?){25}\\d(?!\\d)')
PL_PHONE_CANDIDATE_RE = re.compile('(?<!\\d)(?:(?:\\+48|0048|0)[ \\t().\\-]*)?(?:\\d[ \\t().\\-]*){8}\\d(?!\\d)')
PHONE_CONTEXT_RE = re.compile('(?:telefon(?:u)?|tel\\.?|kom\\.?|komórka|mobile|phone)[^0-9\\n]{0,24}$', re.IGNORECASE)
PHONE_BLOCK_CONTEXT_RE = re.compile('(?:nip|regon|pesel|iban|rachunek|dowód|dokument)\\s*[:\\-]?\\s*$', re.IGNORECASE)

def digits(value: str) -> str:
    return re.sub('\\D', '', value)

def valid_pesel(value: str) -> bool:
    value = digits(value)
    if len(value) != 11:
        return False
    weights = [1, 3, 7, 9, 1, 3, 7, 9, 1, 3]
    total = sum((int(value[i]) * weights[i] for i in range(10)))
    control = (10 - total % 10) % 10
    if control != int(value[10]):
        return False
    yy = int(value[0:2])
    mm = int(value[2:4])
    dd = int(value[4:6])
    if 1 <= mm <= 12:
        year = 1900 + yy
    elif 21 <= mm <= 32:
        year = 2000 + yy
        mm -= 20
    elif 41 <= mm <= 52:
        year = 2100 + yy
        mm -= 40
    elif 61 <= mm <= 72:
        year = 2200 + yy
        mm -= 60
    elif 81 <= mm <= 92:
        year = 1800 + yy
        mm -= 80
    else:
        return False
    try:
        date(year, mm, dd)
    except ValueError:
        return False
    return True

def valid_nip(value: str) -> bool:
    value = digits(value)
    if len(value) != 10:
        return False
    weights = [6, 5, 7, 2, 3, 4, 5, 6, 7]
    checksum = sum((int(value[i]) * weights[i] for i in range(9))) % 11
    if checksum == 10:
        return False
    return checksum == int(value[9])

def valid_regon9(value: str) -> bool:
    if len(value) != 9:
        return False
    weights = [8, 9, 2, 3, 4, 5, 6, 7]
    checksum = sum((int(value[i]) * weights[i] for i in range(8))) % 11
    if checksum == 10:
        checksum = 0
    return checksum == int(value[8])

def valid_regon14(value: str) -> bool:
    if len(value) != 14:
        return False
    if not valid_regon9(value[:9]):
        return False
    weights = [2, 4, 8, 5, 0, 9, 7, 3, 6, 1, 2, 4, 8]
    checksum = sum((int(value[i]) * weights[i] for i in range(13))) % 11
    if checksum == 10:
        checksum = 0
    return checksum == int(value[13])

def valid_regon(value: str) -> bool:
    value = digits(value)
    if len(value) == 9:
        return valid_regon9(value)
    if len(value) == 14:
        return valid_regon14(value)
    return False

def valid_id_card(value: str) -> bool:
    value = re.sub('\\s', '', value).upper()
    if not re.fullmatch('[A-Z]{3}\\d{6}', value):
        return False
    weights = [7, 3, 1, 9, 7, 3, 1, 7, 3]
    values = []
    for char in value[:3]:
        values.append(ord(char) - ord('A') + 10)
    values.extend((int(char) for char in value[3:]))
    checksum = sum((number * weight for number, weight in zip(values, weights)))
    return checksum % 10 == 0

def valid_iban(value: str) -> bool:
    value = re.sub('\\s', '', value).upper()
    if len(value) != 28:
        return False
    if not re.fullmatch('PL\\d{26}', value):
        return False
    rearranged = value[4:] + value[:4]
    numeric = ''
    for char in rearranged:
        if char.isdigit():
            numeric += char
        else:
            numeric += str(ord(char) - ord('A') + 10)
    return int(numeric) % 97 == 1

def valid_nrb(value: str) -> bool:
    value = digits(value)
    if len(value) != 26:
        return False
    return valid_iban('PL' + value)

def normalize_pl_phone(value: str) -> str | None:
    raw = value.strip()
    compact = digits(raw)
    if raw.startswith('+48'):
        normalized = '+' + compact
    elif compact.startswith('0048') and len(compact) == 13:
        normalized = '+' + compact[2:]
    elif compact.startswith('0') and len(compact) == 10:
        normalized = '+48' + compact[1:]
    elif len(compact) == 9:
        normalized = '+48' + compact
    else:
        return None
    try:
        number = phonenumbers.parse(normalized, None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    region = phonenumbers.region_code_for_number(number)
    if region != 'PL':
        return None
    return normalized

class PolishIdentifierDetector:
    source = 'pl_checksum'

    async def detect(self, text: str) -> list[Detection]:
        results = []
        rules = [(PESEL_RE, 'PESEL', valid_pesel), (NIP_RE, 'NIP', valid_nip), (REGON_RE, 'REGON', valid_regon), (ID_CARD_RE, 'ID_CARD', valid_id_card), (PL_IBAN_RE, 'IBAN', valid_iban), (PL_NRB_RE, 'IBAN', valid_nrb)]
        for pattern, entity_type, validator in rules:
            for match in pattern.finditer(text):
                value = match.group(0)
                if not validator(value):
                    continue
                results.append(Detection(start=match.start(), end=match.end(), text=value, entity_type=entity_type, score=1.0, source=self.source))
        for match in PL_PHONE_CANDIDATE_RE.finditer(text):
            candidate = match.group(0)
            normalized_phone = normalize_pl_phone(candidate)
            if normalized_phone is None:
                continue
            prefix = text[max(0, match.start() - 40):match.start()]
            if PHONE_BLOCK_CONTEXT_RE.search(prefix):
                continue
            compact = digits(candidate)
            stripped = candidate.strip()
            has_international_prefix = stripped.startswith('+48') or compact.startswith('0048')
            has_legacy_prefix = compact.startswith('0') and len(compact) == 10 and (not compact.startswith('0048'))
            has_formatting = bool(re.search('[ \\t.\\-()]', stripped))
            has_phone_context = bool(PHONE_CONTEXT_RE.search(prefix))
            if len(compact) == 9 and (not has_formatting) and (not has_phone_context):
                continue
            if has_legacy_prefix and (not has_formatting) and (not has_phone_context):
                continue
            results.append(Detection(start=match.start(), end=match.end(), text=candidate, entity_type='PHONE', score=0.99 if has_international_prefix else 0.97, source='phonenumbers:pl'))
        return results

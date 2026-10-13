# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import ipaddress
import re
from app.detectors.base import Detection
EMAIL_RE = re.compile('\\b[A-Z0-9._%+\\-]+@[A-Z0-9.\\-]+\\.[A-Z]{2,}\\b', re.IGNORECASE)
PESEL_RE = re.compile('(?<!\\d)\\d{11}(?!\\d)')
IPV4_RE = re.compile('(?<![\\d.])(?:\\d{1,3}\\.){3}\\d{1,3}(?![\\d.])')
IBAN_COMPACT_RE = re.compile('\\b[A-Z]{2}\\d{2}[A-Z0-9]{11,30}\\b', re.IGNORECASE)
IBAN_SPACED_RE = re.compile('\\b[A-Z]{2}\\d{2}(?: [A-Z0-9]{4}){2,7}(?: [A-Z0-9]{1,4})?\\b', re.IGNORECASE)
PHONE_INTL_RE = re.compile('(?<![\\w\\d])\\+\\d{1,3}(?:[ \\t().\\-]*\\d){7,14}(?!\\d)')
PHONE_US_RE = re.compile('(?<!\\d)\\(?\\d{3}\\)?[ \\t.\\-]\\d{3}[ \\t.\\-]\\d{4}(?!\\d)')

def valid_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ipaddress.AddressValueError:
        return False

def valid_iban(value: str) -> bool:
    normalized = re.sub('[\\s\\-]', '', value).upper()
    if not 15 <= len(normalized) <= 34:
        return False
    if not re.fullmatch('[A-Z]{2}\\d{2}[A-Z0-9]+', normalized):
        return False
    rearranged = normalized[4:] + normalized[:4]
    numeric = ''
    for char in rearranged:
        if char.isdigit():
            numeric += char
        else:
            numeric += str(ord(char) - ord('A') + 10)
    return int(numeric) % 97 == 1

class RegexDetector:
    source = 'regex'

    async def detect(self, text: str) -> list[Detection]:
        results = []
        rules = [(EMAIL_RE, 'EMAIL', None, 1.0), (IBAN_SPACED_RE, 'IBAN', valid_iban, 1.0), (IBAN_COMPACT_RE, 'IBAN', valid_iban, 1.0), (PESEL_RE, 'PESEL', None, 1.0), (IPV4_RE, 'IP_ADDRESS', valid_ipv4, 1.0), (PHONE_INTL_RE, 'PHONE', None, 0.95), (PHONE_US_RE, 'PHONE', None, 0.9)]
        for pattern, entity_type, validator, score in rules:
            for match in pattern.finditer(text):
                value = match.group(0)
                if validator and (not validator(value)):
                    continue
                results.append(Detection(start=match.start(), end=match.end(), text=value, entity_type=entity_type, score=score, source=self.source))
        return results

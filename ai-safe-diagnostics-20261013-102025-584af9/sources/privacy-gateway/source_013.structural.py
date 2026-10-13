# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import re
from app.detectors.base import Detection
PL_CHARS = 'A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż'
TOKEN = f"[{PL_CHARS}0-9][{PL_CHARS}0-9.'’\\-]*"
HOUSE_NUMBER = '\\d{1,4}[A-Za-z]?(?:/\\d{1,4}[A-Za-z]?)?'
POSTAL_CODE = '\\d{2}-\\d{3}'
CITY_WORD = f'[{PL_CHARS}][{PL_CHARS}\\-]+'
CITY = f'{CITY_WORD}(?:[ \\t]+{CITY_WORD}){{0,3}}'
STREET_PREFIX = '(?:ul(?:ica)?\\.?|al\\.?|aleja|aleje|os\\.?|osiedle|pl\\.?|plac|rynek|rondo|skwer|bulwar)'
STREET_WITH_PREFIX = f'{STREET_PREFIX}[ \\t]+(?:{TOKEN}[ \\t]+){{1,7}}{HOUSE_NUMBER}'
BARE_STREET = f'(?:{TOKEN}[ \\t]+){{1,7}}{HOUSE_NUMBER}'
ADDRESS_SEPARATOR = '(?:[ \\t]*,[ \\t]*|[ \\t]+)'
ADDRESS_LABEL = '(?:adres(?:[ \\t]+(?:zamieszkania|korespondencyjny))?|address)'
CONTEXT_GAP = '[ \\t]*[:\\-]?[ \\t]*(?:\\r?\\n[ \\t]*)?'
FULL_ADDRESS_RE = re.compile(f'(?P<address>{STREET_WITH_PREFIX}{ADDRESS_SEPARATOR}{POSTAL_CODE}[ \\t]+{CITY})', re.IGNORECASE)
CONTEXT_ADDRESS_RE = re.compile(f'{ADDRESS_LABEL}{CONTEXT_GAP}(?P<address>{BARE_STREET}{ADDRESS_SEPARATOR}{POSTAL_CODE}[ \\t]+{CITY})', re.IGNORECASE)
STREET_ADDRESS_RE = re.compile(f'(?P<address>{STREET_WITH_PREFIX})', re.IGNORECASE)
POSTAL_CONTEXT_RE = re.compile(f'(?:kod[ \\t]+pocztowy|postal[ \\t]+code)[ \\t]*[:\\-]?[ \\t]*(?P<postal>{POSTAL_CODE})', re.IGNORECASE)

class PolishAddressDetector:
    source = 'address:pl'

    async def detect(self, text: str) -> list[Detection]:
        results = []
        seen = set()

        def add_detection(start: int, end: int, entity_type: str, score: float):
            key = (start, end, entity_type)
            if key in seen:
                return
            seen.add(key)
            results.append(Detection(start=start, end=end, text=text[start:end], entity_type=entity_type, score=score, source=self.source))
        for match in FULL_ADDRESS_RE.finditer(text):
            add_detection(match.start('address'), match.end('address'), 'ADDRESS', 0.99)
        for match in CONTEXT_ADDRESS_RE.finditer(text):
            add_detection(match.start('address'), match.end('address'), 'ADDRESS', 0.99)
        for match in STREET_ADDRESS_RE.finditer(text):
            add_detection(match.start('address'), match.end('address'), 'ADDRESS', 0.97)
        for match in POSTAL_CONTEXT_RE.finditer(text):
            add_detection(match.start('postal'), match.end('postal'), 'POSTAL_CODE', 0.96)
        return results

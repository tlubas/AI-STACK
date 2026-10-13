# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import re
from lingua import Language, LanguageDetectorBuilder
_detector = LanguageDetectorBuilder.from_languages(Language.POLISH, Language.ENGLISH).build()
SEGMENT_RE = re.compile('.+?(?:[.!?](?=\\s|$)|\\n+|$)', re.DOTALL)

def iter_segments(text: str):
    for match in SEGMENT_RE.finditer(text):
        value = match.group(0)
        if not value.strip():
            continue
        yield (match.start(), match.end(), value)

def detect_language(text: str) -> str | None:
    alpha = ''.join((char for char in text if char.isalpha()))
    if len(alpha) < 3:
        return None
    language = _detector.detect_language_of(text)
    if language == Language.POLISH:
        return 'pl'
    if language == Language.ENGLISH:
        return 'en'
    return None

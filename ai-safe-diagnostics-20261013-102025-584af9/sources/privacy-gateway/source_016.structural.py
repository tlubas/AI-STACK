# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import spacy
from app.detectors.base import Detection
from app.detectors.language import detect_language, iter_segments
PL_ENTITY_MAP = {'persName': 'PERSON', 'placeName': 'LOCATION', 'geogName': 'LOCATION', 'orgName': 'ORGANIZATION'}
EN_ENTITY_MAP = {'PERSON': 'PERSON', 'ORG': 'ORGANIZATION', 'GPE': 'LOCATION', 'LOC': 'LOCATION', 'FAC': 'LOCATION'}
NER_LABEL_STOPWORDS = {'NIP', 'REGON', 'PESEL', 'IBAN', 'EMAIL', 'E-MAIL', 'TELEFON', 'TEL', 'KONTAKT', 'RACHUNEK', 'DOWÓD', 'DOKUMENT', 'ADRES', 'KOD POCZTOWY', 'ULICA', 'UL', 'ALEJA', 'ALEJE', 'OSIEDLE', 'PLAC', 'RYNEK', 'RONDO', 'PHONE', 'CONTACT', 'ADDRESS', 'POSTAL CODE'}
PERSON_COMMAND_WORDS = {'NAPISZ', 'WYŚLIJ', 'PRZEŚLIJ', 'PODAJ', 'POKAŻ', 'SPRAWDŹ', 'ZNAJDŹ', 'DODAJ', 'USUŃ', 'UTWÓRZ', 'ODPOWIEDZ', 'ZADZWOŃ', 'SKONTAKTUJ', 'POWIEDZ', 'PRZYGOTUJ', 'WYGENERUJ', 'WYSZUKAJ', 'WRITE', 'SEND', 'SHOW', 'CHECK', 'FIND', 'CREATE', 'DELETE', 'ADD', 'CALL', 'CONTACT'}
PERSON_COMMAND_LEMMAS = {'napisać', 'wysłać', 'przesłać', 'podać', 'pokazać', 'sprawdzić', 'znaleźć', 'dodać', 'usunąć', 'utworzyć', 'odpowiedzieć', 'zadzwonić', 'skontaktować', 'powiedzieć', 'przygotować', 'wygenerować', 'wyszukać', 'write', 'send', 'show', 'check', 'find', 'create', 'delete', 'add', 'call', 'contact'}
PERSON_TECHNICAL_PREFIXES = {'EMAIL', 'E-MAIL', 'MAIL', 'TELEFON', 'TEL', 'PHONE', 'KONTAKT', 'CONTACT', 'ADRES', 'ADDRESS', 'PESEL', 'NIP', 'REGON', 'IBAN', 'RACHUNEK', 'DOWÓD', 'DOKUMENT'}

class NerDetector:
    source = 'spacy'

    def __init__(self):
        self.nlp_pl = spacy.load('pl_core_news_md')
        self.nlp_en = spacy.load('en_core_web_md')

    def _person_is_plausible(self, ent) -> bool:
        alpha_tokens = [token for token in ent if token.is_alpha]
        if not alpha_tokens:
            return False
        first_surface = alpha_tokens[0].text.strip().rstrip(':').upper()
        if first_surface in PERSON_TECHNICAL_PREFIXES:
            return False
        if len(alpha_tokens) >= 2:
            return any((token.pos_ == 'PROPN' for token in alpha_tokens))
        token = alpha_tokens[0]
        surface = token.text.strip().upper()
        lemma = token.lemma_.strip().lower()
        if surface in PERSON_COMMAND_WORDS:
            return False
        if lemma in PERSON_COMMAND_LEMMAS:
            return False
        if token.pos_ in {'VERB', 'AUX'}:
            return False
        mood = set(token.morph.get('Mood'))
        if 'Imp' in mood:
            return False
        return token.pos_ == 'PROPN'

    def _process_segment(self, text: str, offset: int, language: str) -> list[Detection]:
        if language == 'pl':
            nlp = self.nlp_pl
            mapping = PL_ENTITY_MAP
        else:
            nlp = self.nlp_en
            mapping = EN_ENTITY_MAP
        doc = nlp(text)
        results = []
        for ent in doc.ents:
            normalized = ent.text.strip().rstrip(':').upper()
            if normalized in NER_LABEL_STOPWORDS:
                continue
            entity_type = mapping.get(ent.label_)
            if not entity_type:
                continue
            if entity_type == 'PERSON' and (not self._person_is_plausible(ent)):
                continue
            results.append(Detection(start=offset + ent.start_char, end=offset + ent.end_char, text=ent.text, entity_type=entity_type, score=0.85, source=f'{self.source}:{language}'))
        return results

    async def detect(self, text: str) -> list[Detection]:
        results = []
        for start, _end, segment in iter_segments(text):
            language = detect_language(segment)
            if language is None:
                continue
            results.extend(self._process_segment(segment, start, language))
        return results

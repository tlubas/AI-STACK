# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider
from app.detectors.base import Detection
ENTITY_MAP = {'EMAIL_ADDRESS': 'EMAIL', 'IP_ADDRESS': 'IP_ADDRESS', 'CREDIT_CARD': 'CREDIT_CARD', 'IBAN_CODE': 'IBAN'}
PRESIDIO_ENTITIES = ['EMAIL_ADDRESS', 'IP_ADDRESS', 'CREDIT_CARD', 'IBAN_CODE']

class PresidioDetector:
    source = 'presidio'

    def __init__(self):
        configuration = {'nlp_engine_name': 'spacy', 'models': [{'lang_code': 'en', 'model_name': 'en_core_web_md'}]}
        provider = NlpEngineProvider(nlp_configuration=configuration)
        nlp_engine = provider.create_engine()
        registry = RecognizerRegistry(supported_languages=['en'])
        registry.load_predefined_recognizers()
        self.analyzer = AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=['en'])

    async def detect(self, text: str) -> list[Detection]:
        results = []
        raw = self.analyzer.analyze(text=text, language='en', entities=PRESIDIO_ENTITIES)
        for item in raw:
            entity_type = ENTITY_MAP.get(item.entity_type)
            if not entity_type:
                continue
            results.append(Detection(start=item.start, end=item.end, text=text[item.start:item.end], entity_type=entity_type, score=float(item.score), source=self.source))
        return results

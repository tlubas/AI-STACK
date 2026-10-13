# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
import asyncio
from app.detectors.address_detector import PolishAddressDetector
from app.detectors.ner_detector import NerDetector
from app.detectors.pl_detector import PolishIdentifierDetector
from app.detectors.presidio_detector import PresidioDetector
from app.detectors.regex_detector import RegexDetector
from app.detectors.resolver import resolve_overlaps

class DetectorPipeline:

    def __init__(self):
        self.detectors = [PolishIdentifierDetector(), PolishAddressDetector(), RegexDetector(), PresidioDetector(), NerDetector()]

    async def detect_raw(self, text: str):
        results = await asyncio.gather(*[detector.detect(text) for detector in self.detectors])
        return [item for group in results for item in group]

    async def detect(self, text: str):
        raw = await self.detect_raw(text)
        return resolve_overlaps(raw)
detector_pipeline = DetectorPipeline()

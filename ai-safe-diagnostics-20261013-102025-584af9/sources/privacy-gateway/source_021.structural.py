# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
from app.detectors.base import Detection
ENTITY_PRIORITY = {'PESEL': 110, 'NIP': 110, 'REGON': 110, 'ID_CARD': 110, 'IBAN': 110, 'CREDIT_CARD': 105, 'EMAIL': 100, 'IP_ADDRESS': 95, 'ADDRESS': 94, 'PHONE': 90, 'PERSON': 80, 'LOCATION': 70, 'POSTAL_CODE': 65, 'ORGANIZATION': 60, 'URL': 40}
SOURCE_PRIORITY = {'pl_checksum': 120, 'address:pl': 110, 'regex': 100, 'phonenumbers:pl': 95, 'presidio': 80, 'spacy:pl': 60, 'spacy:en': 60}

def priority(item: Detection):
    return (ENTITY_PRIORITY.get(item.entity_type, 50), item.score, item.length, SOURCE_PRIORITY.get(item.source, 0))

def resolve_overlaps(detections: list[Detection]):
    ordered = sorted(detections, key=priority, reverse=True)
    accepted = []
    for candidate in ordered:
        conflict = any((candidate.overlaps(existing) for existing in accepted))
        if conflict:
            continue
        accepted.append(candidate)
    return sorted(accepted, key=lambda item: (item.start, item.end))

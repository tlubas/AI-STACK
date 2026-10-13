# ANONYMIZED ANALYSIS EXPORT - DO NOT INSTALL
from dataclasses import dataclass

@dataclass(frozen=True)
class Detection:
    start: int
    end: int
    text: str
    entity_type: str
    score: float
    source: str

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: 'Detection') -> bool:
        return self.start < other.end and other.start < self.end

    def to_dict(self):
        return {'start': self.start, 'end': self.end, 'text': self.text, 'entity_type': self.entity_type, 'score': round(self.score, 4), 'source': self.source}

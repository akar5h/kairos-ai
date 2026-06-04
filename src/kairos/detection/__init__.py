"""Stage 3: Failure pattern detection — guard-assertion architecture."""

from kairos.detection.models import Finding
from kairos.detection.runner import detect_tier1

__all__ = [
    "Finding",
    "detect_tier1",
]

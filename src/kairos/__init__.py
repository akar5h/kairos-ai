"""Kairos AI — agent tracing SDK + on-demand failure-clustering engine."""

from kairos.config import settings
from kairos.log import setup_logging

__version__ = "0.1.0"

# Initialize structured logging on import.
setup_logging(
    level=settings.log_level,
    json_output=settings.log_format == "json",
)

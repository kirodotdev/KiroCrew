"""KiroCrew telemetry metrics — public API.

OSS-CLEAN: No internal-only dependencies.
"""

from kiro_crew.metrics.schema import (
    MAX_ATTR_COUNT,
    MAX_ATTR_VALUE_LEN,
    NS_APP_PREFIX,
    NS_CORE,
    NS_GENAI,
    redact,
    validate_attrs,
    validate_name,
)

__all__ = [
    "NS_CORE",
    "NS_GENAI",
    "NS_APP_PREFIX",
    "MAX_ATTR_COUNT",
    "MAX_ATTR_VALUE_LEN",
    "redact",
    "validate_name",
    "validate_attrs",
]

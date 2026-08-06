"""Telemetry namespace + attribute-validation guardrails (contract C4).

These validation and redaction helpers form the thin facade layer in front of
the OpenTelemetry SDK instrument types, which provide neither namespace
enforcement nor PII/secret redaction.

OSS-CLEAN: stdlib only, plus the first-party ``kiro_crew.security`` scrubbers.
PRIVACY: no secrets, PII, or user content in attributes.
CARDINALITY: metric names and attribute VALUES MUST be low-cardinality constants
(enum-like) — never raw ids / timestamps / free-form strings templated into a
name or value. The recorder caches one OTEL instrument per distinct name for the
process lifetime, so unbounded names/values are a cardinality bomb. Attribute
COUNT is capped (MAX_ATTR_COUNT) + string values redacted; hard value-bucketing
and instrument-cache eviction are not yet implemented.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
from typing import Dict, Optional, Union

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# ---------------------------------------------------------------------------
# C4 — Namespace constants
# ---------------------------------------------------------------------------

NS_CORE = "kirocrew."
NS_GENAI = "gen_ai."
NS_APP_PREFIX = "app."  # Full pattern: app.<app_id>.*

# Maximum number of attribute keys allowed per record.
MAX_ATTR_COUNT = 32

# Maximum length for an attribute value (string) before truncation.
MAX_ATTR_VALUE_LEN = 128

# ---------------------------------------------------------------------------
# C4 — Privacy helpers
# ---------------------------------------------------------------------------

# Patterns that indicate high-entropy / credential-shaped values.
_HIGH_ENTROPY_PATTERNS: list[re.Pattern[str]] = [
    # AWS access key ID (AKIA) + STS temporary security credentials (ASIA)
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    # AWS SecretAccessKey= pattern
    re.compile(r"SecretAccessKey\s*=", re.IGNORECASE),
    # Private key headers
    re.compile(r"-----BEGIN\s+\w*\s*PRIVATE\s+KEY-----"),
    # Long hex (40+ chars) — avoids false positives on trace IDs/UUIDs (32 chars)
    re.compile(r"[0-9a-fA-F]{40,}"),
    # JWT-like (three dot-separated base64 segments)
    re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$"),
    # Generic secret patterns (password=, key=, token= prefixed values)
    re.compile(r"(?:password|secret|token|api_key)\s*[:=]", re.IGNORECASE),
]


def _is_high_entropy(value: str) -> bool:
    """Return True if value looks like a credential or high-entropy secret."""
    for pat in _HIGH_ENTROPY_PATTERNS:
        if pat.search(value):
            return True
    # Base64-encoded credential variants: decode and re-check the raw
    # credential patterns on the decoded text (matches redact_credentials()
    # base64 coverage).
    stripped = value.strip()
    if (
        len(stripped) >= 16
        and len(stripped) % 4 == 0
        and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", stripped)
    ):
        try:
            decoded = base64.b64decode(stripped, validate=True).decode(
                "utf-8", "ignore"
            )
        except (binascii.Error, ValueError):
            decoded = ""
        if decoded and decoded != stripped:
            for pat in _HIGH_ENTROPY_PATTERNS:
                if pat.search(decoded):
                    return True
    # Shannon entropy backstop, for values no pattern above matched.
    #
    # Reach: entropy over a string's own character frequencies is bounded by
    # log2(distinct characters), so the 4.5 threshold below is only attainable
    # once a value has at least 23 distinct characters — the length guard lets
    # shorter values in, but they can never cross it. It is also unattainable
    # for any alphabet of fewer than 23 symbols at any length -- hex has 16, so
    # only the 40-or-more pattern above covers it, and shorter hex is
    # deliberately left visible to keep trace ids and UUIDs readable. Keep that
    # in mind before treating this as a general secret detector: it is a
    # last-resort net under the patterns, not a replacement for passing the
    # declared low-cardinality values the CARDINALITY note requires.
    if len(value) >= 16:
        freq: dict[str, int] = {}
        for ch in value:
            freq[ch] = freq.get(ch, 0) + 1
        length = len(value)
        entropy = -sum(
            (c / length) * math.log2(c / length) for c in freq.values()
        )
        # High entropy threshold (4.5 bits/char is suspicious for attribute values)
        if entropy > 4.5:
            return True
    return False


def redact(value: str) -> str:
    """Redact high-entropy or credential-shaped attribute values.

    Returns "[REDACTED]" if the value appears to contain sensitive data.
    Returns the original value truncated to MAX_ATTR_VALUE_LEN for long
    but non-suspicious strings.
    Returns the original value unchanged for short, safe strings.
    """
    # First-party scrubbers (kiro_crew.security): scan for exfiltration URLs
    # AND credentials (incl. base64 variants). Both return (cleaned_text,
    # warnings) tuples — a changed cleaned_text means something sensitive was
    # found. The AKIA/ASIA STS-token shape is also covered by the patterns
    # above.
    if redact_exfiltration_urls(redact_credentials(value)[0])[0] != value:
        return "[REDACTED]"
    if _is_high_entropy(value):
        return "[REDACTED]"
    # Truncate long but non-suspicious strings (not dead code — the entropy
    # check above does NOT trigger for all long strings, only high-entropy ones).
    if len(value) > MAX_ATTR_VALUE_LEN:
        return value[:MAX_ATTR_VALUE_LEN]
    return value


# ---------------------------------------------------------------------------
# C4 — Namespace validation
# ---------------------------------------------------------------------------

# Valid metric name pattern: dotted identifiers.
_METRIC_NAME_RE = re.compile(r"^[a-z][a-z0-9_.]*$")


def validate_name(name: str, *, app_id: Optional[str] = None) -> str:
    """Validate a metric name against C4 namespace rules.

    Args:
        name: Dotted metric name to validate.
        app_id: If provided, the caller is an app; the name MUST start with
                ``app.<app_id>.`` and MUST NOT start with ``kirocrew.`` or
                ``gen_ai.``.

    Returns:
        The validated name (unchanged).

    Raises:
        ValueError: If the name violates namespace rules.
    """
    if not name or not _METRIC_NAME_RE.match(name):
        raise ValueError(
            f"Invalid metric name {name!r}: must be lowercase dotted identifiers"
        )

    if app_id is not None:
        # App callers CANNOT spoof core or gen_ai namespaces.
        if name.startswith(NS_CORE):
            raise ValueError(
                f"App {app_id!r} cannot emit metrics in the kirocrew.* namespace"
            )
        if name.startswith(NS_GENAI):
            raise ValueError(
                f"App {app_id!r} cannot emit metrics in the gen_ai.* namespace"
            )
        # App metrics MUST be prefixed with app.<app_id>.
        expected_prefix = f"app.{app_id}."
        if not name.startswith(expected_prefix):
            raise ValueError(
                f"App {app_id!r} metrics must start with {expected_prefix!r}, "
                f"got {name!r}"
            )
    else:
        # Core callers: must use kirocrew.* or gen_ai.*
        if not (name.startswith(NS_CORE) or name.startswith(NS_GENAI)):
            raise ValueError(
                f"Core metric name must start with {NS_CORE!r} or "
                f"{NS_GENAI!r}, got {name!r}"
            )

    return name


# ---------------------------------------------------------------------------
# C4 — Attribute validation
# ---------------------------------------------------------------------------


def validate_attrs(
    attrs: Dict[str, Union[str, int, bool, float]],
) -> Dict[str, Union[str, int, bool, float]]:
    """Validate and sanitize metric attributes per C4 rules.

    - Caps attribute count at MAX_ATTR_COUNT.
    - Rejects/redacts high-entropy string values (possible secret/PII leak).
    - Truncates long string values.

    Args:
        attrs: Raw attribute dict.

    Returns:
        Sanitized copy of attributes.

    Raises:
        ValueError: If attribute count exceeds MAX_ATTR_COUNT.
    """
    if len(attrs) > MAX_ATTR_COUNT:
        raise ValueError(
            f"Attribute count {len(attrs)} exceeds maximum {MAX_ATTR_COUNT}"
        )

    sanitized: Dict[str, Union[str, int, bool, float]] = {}
    for key, value in attrs.items():
        if isinstance(value, str):
            sanitized[key] = redact(value)
        else:
            # Numeric/bool values are safe by nature (low cardinality).
            sanitized[key] = value

    return sanitized

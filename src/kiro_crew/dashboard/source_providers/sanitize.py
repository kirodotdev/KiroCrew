"""The scrub every piece of provider-controlled text passes before a client sees it.

Provider stderr, a plugin's exception text, and every string in a returned
payload come from outside the trust boundary, so they are all redacted here, by
one pair of redactors, and the normalized payload is held to one byte ceiling.
Keeping the chokepoint in one place is what lets a registered plugin inherit it
by construction.
"""

from __future__ import annotations

import json
import re
from typing import Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# Bound the normalized aggregate returned to the browser. The admission
# reservations in ``cache`` conservatively cover raw bytes, decoded JSON,
# normalized copies, and Python object overhead while a complete direct fetch
# remains alive.
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_SAFE_ERROR_RE = re.compile(r"\s+")


def _safe_error_text(text: str, *, fallback: str = "provider command failed") -> str:
    """Strip credentials and exfiltration URLs from provider error prose.

    Split out from :func:`_safe_error` so a plugin-raised EXCEPTION gets the
    identical treatment a built-in's stderr gets. The two paths must not
    diverge: both end up verbatim in a client-visible response body.
    """
    text = text.strip()
    text = redact_exfiltration_urls(text)[0]
    text = redact_credentials(text)[0]
    text = _SAFE_ERROR_RE.sub(" ", text)
    return text[:600] or fallback


def _safe_error(stderr: bytes) -> str:
    return _safe_error_text(stderr.decode("utf-8", errors="replace"))


def _redact_provider_data(value: Any) -> Any:
    """Recursively redact secrets and suspicious URLs in provider-controlled data."""
    if isinstance(value, str):
        value = redact_exfiltration_urls(value)[0]
        return redact_credentials(value)[0]
    if isinstance(value, list):
        return [_redact_provider_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_provider_data(item) for key, item in value.items()}
    return value


def _payload_size_bytes(data: dict[str, Any]) -> int:
    """Return the compact UTF-8 JSON size used for response and cache bounds."""
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

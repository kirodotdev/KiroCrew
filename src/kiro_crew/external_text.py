"""Redact untrusted text before it reaches operator-visible surfaces."""

from __future__ import annotations

import re

from kiro_crew.platform.context import redact_via_context

_URL_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|api[-_]?key|auth|token|"
    r"password|passwd|secret|signature|sig|credential)"
    r"(=|%3D)(?!\[REDACTED)[^\s&#\"']+"
)


def redact_external_text(text: str) -> str:
    """Apply credential, exfiltration-URL, and secret-parameter redaction."""
    if not text:
        return text
    redacted = redact_via_context(text)
    return _URL_SECRET_PARAM_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted
    )


def external_text_requires_redaction(text: str) -> bool:
    """Return whether operator-visible text would require redaction."""
    return redact_external_text(text) != text

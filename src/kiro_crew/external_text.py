"""Redact untrusted text before it reaches operator-visible surfaces."""

from __future__ import annotations

import re

from kiro_crew.platform.context import redact_via_context

_URL_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|api[-_]?key|auth|token|"
    r"password|passwd|secret|signature|sig|credential)"
    r"(=|%3D)(?!\[REDACTED)[^\s&#\"']+"
)


def redact_url_secret_params(text: str) -> str:
    """Mask the VALUE of a known secret URL parameter in-place.

    The one redaction layer the exfiltration-URL + credential chain does not
    cover: a short credential carried as a URL query parameter
    (``?api_key=abc123``) whose query is under the exfiltration pass's length
    floor and whose value is not credential-shaped, so neither the exfiltration
    pass nor the credential pass alters it. Keyed on the PARAMETER NAME rather
    than the value's shape, so it catches exactly that gap.

    Idempotent: the negative lookahead ``(?!\\[REDACTED)`` skips a parameter
    whose value is already the sentinel, so running it over already-redacted
    text is a no-op. Named and exported so an egress that runs the exfiltration
    + credential chain separately (``eventlog.service._redact_projection_value``)
    can add this same layer without re-running the whole chain.
    """
    if not text:
        return text
    return _URL_SECRET_PARAM_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text
    )


def redact_external_text(text: str) -> str:
    """Apply credential, exfiltration-URL, and secret-parameter redaction."""
    if not text:
        return text
    return redact_url_secret_params(redact_via_context(text))


def external_text_requires_redaction(text: str) -> bool:
    """Return whether operator-visible text would require redaction."""
    return redact_external_text(text) != text

"""Redact untrusted text before it reaches operator-visible surfaces."""

from __future__ import annotations

import re
from collections.abc import Callable

from kiro_crew.platform.context import redact_via_context
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.terminal_safe import normalize_for_scanning, strip_control_characters

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


# One redaction ORDER for untrusted text leaving through a human-facing surface.
# The dashboard's JSON scrub and the ``kirocrew skills`` terminal output both
# print text an agent or a third party wrote, and the order of the passes is the
# security property, so both call ``scrub_untrusted_text`` instead of keeping a copy.
def _redact_untrusted(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def scrub_untrusted_text(
    val: str,
    *,
    strip: Callable[[str], str] = strip_control_characters,
) -> str:
    """Redact ``val``, remove what ``strip`` removes, and redact again.

    Invisible characters split a token, and both redactors decide by matching a
    pattern, so a token split by one matches nothing. The text as stored is
    redacted first, because removing a character can destroy a boundary a
    pattern needs. Removing a control character can also JOIN a split token, so
    a changed copy is redacted again.

    ``strip`` is the one surface-specific step. The dashboard drops control
    characters and keeps the text around them; the terminal drops whole escape
    sequences, whose parameter bytes would otherwise print as residue.

    Format characters (a soft hyphen, a joiner) are usually content, so a copy
    with them removed is scanned only as evidence. That copy is returned only
    when it reveals a credential the output still hides.
    """
    out = _redact_untrusted(val)
    stripped = strip(out)
    if stripped != out:
        out = _redact_untrusted(stripped)
    normalised = normalize_for_scanning(out)
    if normalised == out:
        return out
    scanned = _redact_untrusted(normalised)
    if scanned == normalised:
        return out
    return scanned

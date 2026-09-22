"""One redaction order for untrusted text leaving through a human-facing surface.

The dashboard's JSON scrub and the ``kirocrew skills`` terminal output both
print text an agent or a third party wrote. The ORDER of the passes below is
the security property, so both surfaces call this function instead of keeping
their own copy of it.
"""

from __future__ import annotations

from collections.abc import Callable

from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.terminal_safe import normalize_for_scanning, strip_control_characters


def _redact(text: str) -> str:
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
    out = _redact(val)
    stripped = strip(out)
    if stripped != out:
        out = _redact(stripped)
    normalised = normalize_for_scanning(out)
    if normalised == out:
        return out
    scanned = _redact(normalised)
    if scanned == normalised:
        return out
    return scanned

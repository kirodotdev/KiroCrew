"""The display label a ``spawn_run`` caller may give each task.

The task box and the launch receipt name a run by its task text, which for an
orchestrator's assignment is usually a long prompt. A label is a short name the
caller chooses for the row instead. It is display only: the sub-agent still
receives the task text unchanged, and nothing routes or matches on the label.
"""

from __future__ import annotations

#: The most characters a label keeps. The task box shows one line per run, so a
#: longer label is cut rather than wrapped.
SPAWN_LABEL_MAX_CHARS = 80


def normalize_spawn_label(value: object) -> str:
    """*value* as one bounded display line, or ``""`` when there is none.

    Credentials and exfiltration URLs are redacted from the WHOLE value first:
    a cut taken before redaction could split a key so that the part left in
    the label matches no pattern a later redactor knows. Then
    control characters (newlines and tabs included) become spaces, whitespace
    runs collapse to one space, and the result is cut to
    :data:`SPAWN_LABEL_MAX_CHARS`. A non-string yields ``""``, so a malformed
    field falls back to the task text instead of failing the spawn.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    # Imported here: ``SubagentInfo`` calls this on construction, and the
    # security package is too large to load for every importer of this module.
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    text, _ = redact_exfiltration_urls(value)
    text, _ = redact_credentials(text)
    printable = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(printable.split())[:SPAWN_LABEL_MAX_CHARS].rstrip()

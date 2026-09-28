"""The one redaction sequence for text the backup engine publishes or renders.

A label this install publishes to the drive, a label read back from another install,
a recorded nightly failure message the status route serves, and a conversation row
the sessions archive exports all pass through :func:`_redact_egress`. There is one
copy, because a second copy of the sequence is how one caller gains a redactor the
other never gets.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

#: Ceiling on a rendered label. A label written by ANOTHER install is
#: foreign-authored text arriving through the same door object names arrive
#: through, so it is bounded before it is rendered; the row it lands in is one
#: line of 12px caption.
LABEL_MAX_CHARS = 64


def _redact_egress(text: str) -> str:
    """The two egress redactors, in one place, for everything the backup engine ships.

    Both callers need the same SEQUENCE and nothing else in common:
    :func:`sanitize_label` wraps it in a printable filter and a length bound, and
    :func:`_redacted_row` applies it per text column of the conversation export.
    Keeping the sequence here is the point -- :func:`sanitize_label`'s own note says a
    second copy of it is how one copy gains a redactor the other never gets, and an
    export that missed a redactor the labels have would ship the thing it was added
    for.

    The two answer different questions and both are needed: one removes credential
    SHAPES, the other removes URLs whose destination would exfiltrate. A conversation
    holds both, because a model was shown a key and was asked to POST somewhere.
    """
    text, _ = redact_credentials(text)
    text, _ = redact_exfiltration_urls(text)
    return text


def sanitize_label(label: Any, *, fallback: str = "", limit: int = LABEL_MAX_CHARS) -> str:
    """A label safe to render, from a value that may not be ours.

    Applied to a label read from the BUCKET, where the writer is another install
    and the bytes are foreign-authored text. ``storage.list_section`` already runs
    object names through these same two redactors for exactly this reason -- a key
    authored outside this app can embed a credential or a beacon URL -- and a
    label arrives through the same door with the same problem, so it gets the same
    treatment rather than a weaker one because it is "just a name".

    Control characters go first: they are what turns one caption line into
    something that overwrites the row above it, and they survive both redactors
    untouched. Then the two egress redactors, then the length bound.

    Applied to the LOCAL label too, on the way in. The owner types that one, so it
    is not hostile -- but it is the string this install publishes to a drive
    another install reads, and a value that would be scrubbed on arrival has no
    business being sent.

    ``limit`` exists because a second caller needs the same pipeline with a longer
    bound: a recorded failure message is a diagnostic, not a caption, and 64
    characters cuts it mid-sentence. Only the bound varies, so only the bound is a
    parameter -- a second copy of the filter-and-redact sequence is how one copy
    gains a redactor the other never gets.
    """
    if not isinstance(label, str):
        return fallback
    text = "".join(ch for ch in label if ch.isprintable()).strip()
    if not text:
        return fallback
    text = _redact_egress(text)
    text = text.strip()
    if not text:
        return fallback
    return text[:limit]

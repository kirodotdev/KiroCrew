"""Publish a child's stderr tail only when no credential could hide in it.

A harness's stderr reaches the process-exit ``AcpError`` and the runtime's death
summary, and from there the dashboard, chat cards, persisted errors and bug
reports. It is untrusted subprocess output that can echo a credential.

Redacting the JOINED tail is not enough. The ring buffer holds one entry per
line, so a secret the harness wrapped across two lines comes back with the
separator inside it, and the bare-secret patterns (a contiguous 40-character
run) do not match it: every character of the key is published, and deleting the
separator recovers it whole.

So the tail is offered to the redactor in BOTH spellings: as published, and with
the line breaks removed so an interrupted run rejoins. When either spelling is
rewritten, the tail is withheld entirely and a fixed marker says so. Only a tail
neither spelling flags is published, and it is bounded in characters, because
the ring buffer bounds lines, not their length.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Published in place of a tail the redactor flagged in either spelling.
STDERR_WITHHELD = "stderr withheld: redacted content"

#: Upper bound on a published tail, in characters. The ring buffer bounds the
#: LINE count only, so twenty long lines could otherwise make a very large error
#: string. The cut keeps the end, where a harness writes its verdict.
STDERR_TAIL_MAX_CHARS = 2000


def safe_stderr_tail(
    lines: Iterable[str], sep: str, *, max_chars: int = STDERR_TAIL_MAX_CHARS
) -> str:
    """*lines* joined with *sep*, or :data:`STDERR_WITHHELD`, or ``""``.

    ``""`` when there is nothing to publish. Both spellings are checked whole and
    the bound is applied only to a tail that passed, so a secret straddling the
    cut is judged before the cut can leave an unrecognisable fragment of it.
    """
    from kiro_crew.security import redact

    kept = [line for line in lines if isinstance(line, str)]
    if not kept:
        return ""
    joined = sep.join(kept)
    if not joined.strip():
        return ""
    # The separator-free spelling: the characters a reader recovers by deleting
    # the separators. Rebuilt from the lines rather than by deleting *sep* from
    # *joined*, so a separator that also occurs INSIDE a line is not mistaken for
    # a line break.
    rejoined = "".join(kept)
    # Both spellings are judged WHOLE, before any cut: a cut can start the window
    # mid-secret, leaving a fragment with neither its prefix nor its full run,
    # which no pattern recognises.
    if redact(joined) != joined or redact(rejoined) != rejoined:
        return STDERR_WITHHELD
    return joined[-max_chars:]

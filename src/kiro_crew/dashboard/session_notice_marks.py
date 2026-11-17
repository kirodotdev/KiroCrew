"""Which session-start notices a chat already showed.

A chat starts a new backend session on every reconnect, reload and restart, and a
notice about the agent's spec (a hook that does not run, a key nothing carries)
would otherwise be added again each time although nothing changed. The chat keeps
a short list of marks, one per notice it showed, and shows a notice again only
when its text or the folder it is about differs. The list is saved on the chat's
metadata line (``session_notice_marks``), so a restart keeps it.
"""

from __future__ import annotations

import zlib
from typing import Any

#: How many marks a chat keeps; the oldest is dropped first.
SESSION_NOTICE_MARKS = 32


def session_notice_mark(text: str, *key: object) -> str:
    """The 16-hex-digit mark of a notice's *text* plus the folder *key* it is about.

    A checksum, not a cryptographic hash: the mark only tells one shown notice
    from another, and a collision only hides a repeated informational row.
    """
    body = "\0".join([text, *(str(part) for part in key)]).encode("utf-8", "surrogatepass")
    return f"{zlib.crc32(body):08x}{zlib.adler32(body):08x}"


def _session_notice_is_new(slot: Any, text: str, *key: object) -> bool:
    """Whether *slot*'s chat has yet to show this notice; marks it shown.

    A slot that keeps no marks (a test double, a slot-less caller) always shows it.
    """
    marks = getattr(slot, "_session_notice_marks", None)
    if not isinstance(marks, list):
        return True
    mark = session_notice_mark(text, *key)
    if mark in marks:
        return False
    slot._session_notice_marks = [*marks, mark][-SESSION_NOTICE_MARKS:]
    return True

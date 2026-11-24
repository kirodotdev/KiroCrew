"""The bounded display scan every channel's approval prompt runs over model-authored text.

An approval prompt shows only the start of a tool's title, purpose and input, so the
display-form redaction need read only that much: :func:`bounded_scan` reads a bounded
head. :data:`_BACKTICK_PAIR_RE` is the zero-width-space break both channels apply so
no backtick in the input can close the code fence it is shown in. Shared here so the
Slack card and the Discord prompt cannot drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: A backtick followed by another. Each gets a zero-width space after it, so no
#: two backticks stay adjacent inside the fence and none can close it. The space
#: is invisible, so the approver reads the arguments as they were sent.
_BACKTICK_PAIR_RE = re.compile(r"`(?=`)")

#: How much of one model-authored string the prompt's display scan reads. A prompt
#: shows at most a few thousand characters of each, so a 500 KB write costs the scan
#: what a 16 KB one does, on the event loop every turn shares.
_SCAN_CHARS = 16 * 1024


def bounded_scan(text: str, scan: Callable[[str], str]) -> str:
    """*scan* (the renderer's display-form redaction) of the part of *text* the prompt shows.

    A longer *text* is scanned as its first :data:`_SCAN_CHARS` characters, and the
    prompt shows only the start of those. The cut can split a key at the head's end,
    leaving its first part unredacted there; that end stays off the prompt while the
    scanned head is over half the bound. A head the scan shrank further (stripped
    escapes, a long secret's short tag) is answered by a scan of the whole text.
    """
    if len(text) <= _SCAN_CHARS:
        return scan(text)
    head = scan(text[:_SCAN_CHARS])
    return head if len(head) > _SCAN_CHARS // 2 else scan(text)

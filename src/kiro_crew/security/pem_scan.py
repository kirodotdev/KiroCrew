"""The PEM private-key spellings the credential scan shares, and the lookup that keeps it linear.

``redaction._CREDENTIAL_PATTERNS`` spells a PEM private key as a header followed by either a
full block (the body, lazily, up to the first END marker) or a truncated tail. The full block
fails only after reading to the end of the text, so a header that no END marker follows costs
one such read, and a text that repeats the header costs one per header: quadratic, with the
GIL held.

The full block can succeed only when an END marker starts at or after the header's end, so
one lookup of the text's last marker settles it for every header. The scan then matches a
header past that marker with the alternation minus the full-block tail, which gives the same
match because the tail cannot succeed there.
"""

from __future__ import annotations

import re

#: A PEM private-key header, as the credential alternation spells it.
HEADER = r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
#: A PEM private-key END marker.
END = r"-----END [A-Z ]*PRIVATE KEY-----"
#: The alternation's full-block tail: the body, lazily, up to the first END marker.
FULL_BLOCK = rf"[\s\S]*?{END}"

#: The literal every PEM private-key header starts with.
HEADER_PREFIX = "-----BEGIN "
_END_PREFIX = "-----END "
_HEADER_RE = re.compile(HEADER)
_END_RE = re.compile(END)


def last_end_start(text: str) -> int:
    """Start of the last PEM END marker in *text*, else ``-1``; overlapping markers count.

    Walks back over the ``-----END `` prefixes, each checked once, so it is linear.
    """
    end = len(text)
    while (at := text.rfind(_END_PREFIX, 0, end)) != -1:
        if _END_RE.match(text, at):
            return at
        end = at + len(_END_PREFIX) - 1
    return -1


def has_no_end_after(text: str, at: int, last_end: int) -> bool:
    """Whether *text* has a PEM header at *at* that no END marker follows.

    *last_end* is :func:`last_end_start` of *text*. The full-block tail needs a marker
    starting at or after the header's end, so the last marker alone says whether one exists.
    False when there is no PEM header at *at*.
    """
    if not text.startswith(HEADER_PREFIX, at):
        return False
    header = _HEADER_RE.match(text, at)
    return header is not None and last_end < header.end()

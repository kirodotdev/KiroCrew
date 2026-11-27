"""Where kiro-cli's inline steer acknowledgement ends.

kiro-cli folds a mid-turn steer into the turn and the model acknowledges it
inline as ``[STEERING steer-<id>: <summary>]``. The summary is the model's own
words, so it can hold a markdown link such as ``[PR #7](https://...)``. A frame
therefore ends at the ``]`` that balances its own ``[``. A summary whose
brackets never balance ends at the first ``]`` after the head, where every
reader ended it before. An unclosed frame is no frame.

Every Python reader of the frame takes its end from here: the channel stream
filter and whole-text strip in ``messaging.driver``, the Discord and Telegram
renderers, and the sidebar preview. The dashboard's own parser,
``website/src/app-sdk/protocol/steering.ts``, applies the same rule.
"""

from __future__ import annotations

import re
from typing import NamedTuple

STEER_PREFIX = "[STEERING"
# No ``\s*`` after the colon: the lazy group already admits that whitespace, and
# two quantifiers that can both take it make a failed match quadratic. The
# readers of the group strip it.
_FRAME = r"\[STEERING\s+steer-[0-9a-f-]+(?:\s*:(.*?))?\]"
#: One closed frame, matched against a whole candidate string.
STEER_MARKER_RE = re.compile(rf"^{_FRAME}$", re.IGNORECASE | re.DOTALL)
# The frame's head up to the end of its id. Nothing it matches can be a bracket,
# so a frame's own ``]`` is never inside it; what follows the id is read by hand
# in ``_summary_of`` rather than by a pattern that runs on to the ``]``.
_HEAD_RE = re.compile(r"\[STEERING\s+steer-[0-9a-f-]+", re.IGNORECASE)
_PREFIX_SEARCH = re.compile(re.escape(STEER_PREFIX), re.IGNORECASE)
_BRACKET_RE = re.compile(r"[\[\]]")


class SteerAck(NamedTuple):
    start: int
    #: One past the frame's closing ``]``.
    end: int
    #: The text after the colon, untrimmed; empty for a frame with none.
    summary: str


def balanced_close(text: str, start: int) -> int:
    """Index of the ``]`` that balances the ``[`` at *start*, or ``-1``."""
    depth = 0
    for bracket in _BRACKET_RE.finditer(text, start):
        depth += 1 if bracket.group() == "[" else -1
        if depth == 0:
            return bracket.start()
    return -1


def _balanced_closers(text: str) -> dict[int, int]:
    """The balancing ``]`` of every ``[`` that has one, in one pass over *text*."""
    closers: dict[int, int] = {}
    openers: list[int] = []
    for bracket in _BRACKET_RE.finditer(text):
        if bracket.group() == "[":
            openers.append(bracket.start())
        elif openers:
            closers[openers.pop()] = bracket.start()
    return closers


def find_steering_acks(text: str, *, unbalanced: bool = True) -> list[SteerAck]:
    """Every complete frame in *text*, left to right; a frame inside another is part of it.

    ``unbalanced=False`` leaves out a frame whose brackets never balance. A reader
    that acts on a frame for good while the text is still arriving passes it,
    because the first ``]`` there may close a link inside a frame whose own ``]``
    has not arrived yet.
    """
    acks: list[SteerAck] = []
    closers: dict[int, int] | None = None
    # The first "]" at or after the last head that needed one. Heads only move
    # right, so it is searched for again only once a head passes it, which keeps
    # a run of unbalanced heads linear.
    next_close = -1
    pos = 0
    # A search, not ``text.upper().find``: upper-casing can change the length
    # (``ß`` -> ``SS``), which would shift every index after it.
    while (hit := _PREFIX_SEARCH.search(text, pos)) is not None:
        start = hit.start()
        if closers is None:
            closers = _balanced_closers(text)
        close = closers.get(start, -1)
        if close < 0 and unbalanced:
            if next_close < start:
                next_close = text.find("]", start)
                if next_close < 0:
                    break
            close = next_close
        summary = _summary_of(text, start, close) if close >= 0 else None
        if summary is None:
            pos = start + 1
            continue
        acks.append(SteerAck(start, close + 1, summary))
        pos = close + 1
    return acks


def _summary_of(text: str, start: int, close: int) -> str | None:
    """The summary of the frame at ``text[start : close + 1]``, or None when it is no frame.

    The frame is ``STEER_MARKER_RE``'s grammar: the head and id, then the ``]``
    at once, or optional whitespace, a ``:`` and the summary up to the ``]``.
    """
    head = _HEAD_RE.match(text, start, close)
    if head is None:
        return None
    if head.end() == close:
        return ""
    colon = head.end()
    while colon < close and text[colon].isspace():
        colon += 1
    if colon < close and text[colon] == ":":
        return text[colon + 1 : close]
    return None


def strip_steering_acks(text: str) -> str:
    """*text* without its complete frames; an unclosed ``[STEER…`` tail is the text's own."""
    out: list[str] = []
    pos = 0
    for ack in find_steering_acks(text):
        out.append(text[pos : ack.start])
        pos = ack.end
    out.append(text[pos:])
    return "".join(out)

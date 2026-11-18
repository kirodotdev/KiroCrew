"""The line walk that decides which ``[OPTIONS:]`` markers share a line.

The line pattern in :mod:`kiro_crew.constants` needs the line end, so it finds
only a line's LAST marker. This walk decides the rest of that line without a
regex: a marker inside a head that never closed is refused, and the sibling
markers running up to the last one are read with it as one run. The web
dashboard's parser walks a line the same way. A line ends at any of
:data:`LINE_BREAKS`, the web parser's set; the matcher hands this walk and the
line pattern text with each of them written as ``\\n`` (:func:`as_newlines`).

The bracket tables and the sibling check are passed in, so this module imports
nothing from the grammar it serves.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

#: Longest first, so ``[OPTIONS:`` is not read as ``[OPTION:`` plus ``S``.
#: Compared case-folded by :func:`head_at`; the same table as the web parser's.
MARKER_HEADS = ("[options:", "[option:")
_HEAD_FRAME = -1
#: Heads one message may carry before EVERY marker in it is refused, which
#: bounds the per-message work. The same cap as the web parser's.
HEAD_CAP = 256
#: The characters that end a line: the web parser's ``isLineBreak`` set.
LINE_BREAKS = "\n\r\u2028\u2029"
_AS_NEWLINE = str.maketrans(dict.fromkeys(LINE_BREAKS[1:], "\n"))


def as_newlines(text: str) -> str:
    """*text* with every line break written as ``\\n``, so no offset moves."""
    if any(br in text for br in LINE_BREAKS[1:]):
        return text.translate(_AS_NEWLINE)
    return text


def line_start(text: str, end: int) -> int:
    """Where the line holding *end* starts: just past the last break before it."""
    return max(text.rfind(br, 0, end) for br in LINE_BREAKS) + 1


def head_at(text: str, i: int) -> int:
    """Length of the head starting at *i*, in any ASCII casing, else 0."""
    for head in MARKER_HEADS:
        if len(text) - i < len(head):
            continue
        if all(text[i + k] in (ch, ch.upper()) for k, ch in enumerate(head)):
            return len(head)
    return 0


def head_count(text: str) -> int:
    """How many marker heads *text* holds, in any casing."""
    folded = text.lower()
    return sum(folded.count(head) for head in MARKER_HEADS)


def option_labels(labels: str) -> list[str]:
    """The choices a marker's label text offers, in order.

    Split on ``|`` when the text holds one, else on ``,``: the web parser's
    ``optionsOf`` rule, so a pipe keeps a comma inside a label whole.
    """
    sep = "|" if "|" in labels else ","
    return [o.strip() for o in labels.split(sep) if o.strip()]


def closer_continues(text: str, i: int, openers: str, closers: str) -> bool:
    """Whether the closer at *i* is label text because the list goes on after it.

    It goes on when another closer follows at once, or a ``|`` after blanks. A
    ``,`` is also prose punctuation, so it counts only when the next bracket on
    the line is a closer: ``Use [OPTIONS: A | B], then pick [OPTIONS: Yes | No]``
    ends at the first ``]``.
    """
    n = len(text)
    j = i + 1
    if j < n and text[j] in closers:
        return True
    while j < n and text[j] in " \t":
        j += 1
    if j < n and text[j] == "|":
        return True
    if j >= n or text[j] != ",":
        return False
    j += 1
    while j < n and text[j] != "\n":
        if text[j] in closers:
            return True
        if text[j] in openers:
            return False
        j += 1
    return False


def after_link_close(text: str, j: int) -> int:
    """Past a stray link close glued to a closer (``](OPTIONS)``), else *j*."""
    if j >= len(text) or text[j] != "(":
        return j
    k = j + 1
    while k < len(text) and text[k] not in "()" and not text[k].isspace():
        k += 1
    return k + 1 if k < len(text) and text[k] == ")" else j


def code_spans(text: str, start: int, end: int) -> dict[int, tuple[int, int]]:
    """Each backtick run in ``text[start:end]``, by start: its length and its span's end.

    A run of N backticks opens an inline code span that closes at the next run of
    exactly N, as Markdown reads it. The span's end is the index past that closing
    run, or -1 when no such run follows before *end*, so the run is plain text. A
    backslash escapes only a run's first backtick: that backtick is plain text and
    the N-1 after it may open a span. The whole run still closes one, because a
    backslash inside code is literal.
    """
    runs: list[tuple[int, int, bool]] = []
    slashes = 0
    i = start
    while i < end:
        if text[i] != "`":
            slashes = slashes + 1 if text[i] == "\\" else 0
            i += 1
            continue
        j = i + 1
        while j < end and text[j] == "`":
            j += 1
        runs.append((i, j - i, slashes % 2 == 1))
        slashes = 0
        i = j
    spans: dict[int, tuple[int, int]] = {}
    next_close: dict[int, int] = {}
    for pos, width, escaped in reversed(runs):
        if escaped:
            spans[pos] = (1, -1)
            if width > 1:
                spans[pos + 1] = (width - 1, next_close.get(width - 1, -1))
        else:
            spans[pos] = (width, next_close.get(width, -1))
        next_close[width] = pos + width
    return spans


def markers_before(
    text: str,
    end: int,
    openers: str,
    closers: str,
    sibling: Callable[[int, int], re.Match[str] | None],
) -> list[re.Match[str]] | None:
    """The sibling markers running up to the marker at *end* on its line.

    Returns ``None`` when *end* sits inside an earlier head that never closed,
    so ``[OPTIONS: dropped [OPTIONS: A | B]`` is no marker at all. The walk goes
    once, left to right, with a stack of brackets typed by *openers* and
    *closers* (index-aligned pairs):

    * outside every head, an inline code span is skipped whole, so the head in
      ``The `[OPTIONS:` line goes last. [OPTIONS: A | B]`` is code, not a head;
      inside a head, backticks are label text, as the label grammar reads them;
    * a head opens a head frame; an opener opens its own frame;
    * a closer pops an opener frame only of its own type;
    * a closer pops a head frame unless the list goes on after it.

    When an outermost head closes and blanks then another head follow,
    ``sibling(head, next_head)`` decides whether that span is a marker. Siblings
    count only as an unbroken run that ends at *end*; anything else leaves them
    as text.
    """
    stack: list[int] = []
    depth = 0
    head = -1
    run: list[re.Match[str]] = []
    run_end = -1
    i = text.rfind("\n", 0, end) + 1
    spans = code_spans(text, i, end)
    while i < end:
        ch = text[i]
        if ch == "`" and depth == 0 and i in spans:
            width, span_end = spans[i]
            i = span_end if span_end >= 0 else i + width
            continue
        width = head_at(text, i) if ch == "[" else 0
        if width:
            if depth > 0 or i != run_end:
                run = []
            if depth == 0:
                head = i
            stack.append(_HEAD_FRAME)
            depth += 1
            i += width
            continue
        i += 1
        opener = openers.find(ch)
        if opener >= 0:
            stack.append(opener)
            continue
        closer = closers.find(ch)
        if closer < 0 or not stack:
            continue
        if stack[-1] != _HEAD_FRAME:
            if stack[-1] == closer:
                stack.pop()
            continue
        if closer_continues(text, i - 1, openers, closers):
            continue
        stack.pop()
        depth -= 1
        if depth > 0:
            continue
        nxt = after_link_close(text, i)
        while nxt < len(text) and text[nxt] in " \t":
            nxt += 1
        found = sibling(head, nxt) if head_at(text, nxt) else None
        if found:
            run.append(found)
            run_end = nxt
        else:
            run = []
            run_end = -1
    if depth > 0:
        return None
    return run if run_end == end else []


class MarkerRun:
    """One line's run of markers, read as a single match.

    It spans the first sibling to the last marker, and its groups are the last
    marker's, because the last marker is the one whose options a turn offers.
    The matches may come from :func:`as_newlines`' copy; *string* is the text
    the groups are read from, so a label keeps its own line breaks.
    """

    __slots__ = ("_first", "_last", "_string")

    def __init__(
        self, first: re.Match[str], last: re.Match[str], string: str | None = None
    ) -> None:
        self._first = first
        self._last = last
        self._string = last.string if string is None else string

    @property
    def string(self) -> str:
        return self._string

    def start(self) -> int:
        return self._first.start()

    def end(self) -> int:
        return self._last.end()

    def span(self) -> tuple[int, int]:
        return self.start(), self.end()

    def group(self, name: str | int = 0) -> str | Any:
        if name == 0:
            return self.string[self.start() : self.end()]
        start, end = self._last.span(name)
        return None if start < 0 else self.string[start:end]

    def groupdict(self) -> dict[str, str | Any]:
        return {name: self.group(name) for name in self._last.groupdict()}

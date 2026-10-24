"""Input normalisation for the credential redactor.

The same secret reaches the redactor in many spellings: percent-encoded in a
URL, JSON-escaped inside a string, split by a terminal colour code or a
zero-width character. The rules match the plain spelling, so the redactor also
runs them over a NORMALISED VIEW of the text and maps every span it finds there
back to the original characters. Nothing is rewritten in place: the view exists
only to be matched against.

A view is built in up to :data:`_MAX_ROUNDS` rounds, so a value encoded twice
(JSON inside JSON, a percent sign percent-encoded) is still read. Each round
does, left to right:

* ``%XX`` for an ASCII byte becomes that character;
* a JSON escape (``\\"``, ``\\\\``, ``\\/``, ``\\n``, ``\\r``, ``\\t``,
  ``\\uXXXX``) becomes the character it stands for;
* a terminal escape sequence (``ESC [ ... m`` and the other CSI forms), every
  other control character except tab, newline and carriage return, and the
  invisible format characters (zero-width space, joiners, word joiner, byte
  order mark, soft hyphen) are dropped.

:mod:`kiro_crew.security.redaction` is the import path callers use.
"""

from __future__ import annotations

import re
import threading
from typing import NamedTuple

#: A round's tokens. Group ``pct``: a percent-encoded ASCII byte. Groups ``uni``
#: and ``esc``: a JSON escape. Group ``drop``: characters that are removed.
_TOKEN_RE = re.compile(
    r"%(?P<pct>[0-7][0-9A-Fa-f])"
    r"|\\(?:u(?P<uni>[0-9A-Fa-f]{4})|(?P<esc>[\"\\/nrtbf]))"
    r"|(?P<drop>\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
    r"|[\u00ad\u200b-\u200d\u2060\ufeff])"
)

_PCT_ALL = r"%(?P<pct>[0-7][0-9A-Fa-f])"
#: An encoded quote, backslash, ``&``, ``,`` or ``;`` stays as written.
_PCT_KEEP = r"%(?!2[2267Cc]|3[Bb]|5[Cc])(?P<pct>[0-7][0-9A-Fa-f])"
#: An encoded space or tab stays as written: decoded, a blank ends a value
#: (``password%3DTwo%20Words``) as surely as a quote does. A chain of its own,
#: so an encoded ``&`` after the value still ends it there.
_PCT_KEEP_BLANK = r"%(?!20|09)(?P<pct>[0-7][0-9A-Fa-f])"
_JSON_ALL = r"\\(?:u(?P<uni>[0-9A-Fa-f]{4})|(?P<esc>[\"\\/nrtbf]))"
#: A JSON-escaped quote or backslash (``\\"``, ``\\\\``, ``\\u0022``,
#: ``\\u0027``, ``\\u005c``) stays as written.
_JSON_KEEP = r"\\(?:u(?!00(?:2[27]|5[Cc]))(?P<uni>[0-9A-Fa-f]{4})|(?P<esc>[/nrtbf]))"
#: A JSON-escaped line break or tab (``\\n``, ``\\r``, ``\\t`` and their
#: ``\\u`` forms) stays as written: decoded, it ends a value (a password with
#: an escaped newline) at the break. A chain of its own, so the label is still
#: decoded beside it (``pass\\u0077ord``).
_JSON_KEEP_BREAK = r"\\(?:u(?!00(?:0[9AaDd]))(?P<uni>[0-9A-Fa-f]{4})|(?P<esc>[\"\\/bf]))"
_DROP = (
    r"(?P<drop>\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
    r"|[\u00ad\u200b-\u200d\u2060\ufeff])"
)

#: The decode chains every reader sees, the full decode first. Decoded, a value
#: delimiter inside a value would end it (or close its quote) and leave the rest
#: in the clear, so the other chains keep the percent-encoded ones, the
#: JSON-escaped ones, or both, as written: each value is read whole by some
#: chain while a label spelled with them is still found by the full one.
_CHAINS = tuple(
    re.compile(f"{pct}|{js}|{_DROP}")
    for pct, js in (
        (_PCT_ALL, _JSON_ALL),
        (_PCT_KEEP, _JSON_ALL),
        (_PCT_ALL, _JSON_KEEP),
        (_PCT_KEEP, _JSON_KEEP),
        (_PCT_KEEP_BLANK, _JSON_ALL),
        (_PCT_ALL, _JSON_KEEP_BREAK),
    )
)


_JSON_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
}

#: Rounds of decoding: enough for a value escaped twice plus one more layer.
_MAX_ROUNDS = 3


class View(NamedTuple):
    """A normalised view of a text and, per view character, where it came from.

    View character ``i`` was built from the source characters
    ``starts[i]:ends[i]``. A dropped character has no view character; a span of
    the view therefore maps to the source range from its first character's start
    to its last character's end, which covers anything dropped between them.
    """

    text: str
    starts: list[int]
    ends: list[int]

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """The source characters a non-empty view span ``[start, end)`` covers."""
        return self.starts[start], self.ends[end - 1]


def _one_round(text: str, tokens: re.Pattern[str] = _TOKEN_RE) -> View | None:
    """One left-to-right decode of *text*, or None when it changes nothing."""
    parts: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    cursor = 0
    changed = False
    for m in tokens.finditer(text):
        changed = True
        if m.start() > cursor:
            parts.append(text[cursor : m.start()])
            starts.extend(range(cursor, m.start()))
            ends.extend(range(cursor + 1, m.start() + 1))
        if m["pct"] is not None:
            out = chr(int(m["pct"], 16))
        elif m["uni"] is not None:
            out = chr(int(m["uni"], 16))
        elif m["esc"] is not None:
            out = _JSON_ESCAPES[m["esc"]]
        else:
            out = ""
        if out:
            parts.append(out)
            starts.append(m.start())
            ends.append(m.end())
        cursor = m.end()
    if not changed:
        return None
    if cursor < len(text):
        parts.append(text[cursor:])
        starts.extend(range(cursor, len(text)))
        ends.extend(range(cursor + 1, len(text) + 1))
    return View("".join(parts), starts, ends)


def _decode_chain(text: str, tokens: re.Pattern[str]) -> list[View]:
    """The decoded layers of *text* under *tokens*, first round first, each
    mapped straight to *text*."""
    layers: list[View] = []
    view: View | None = None
    for _ in range(_MAX_ROUNDS):
        layer = _one_round(text if view is None else view.text, tokens)
        if layer is None:
            break
        if view is not None:
            # Map the new layer straight to the original text.
            layer = View(
                layer.text,
                [view.starts[i] for i in layer.starts],
                [view.ends[i - 1] for i in layer.ends],
            )
        view = layer
        if view.text:
            layers.append(view)
    return layers


def normalised_views(text: str) -> list[View]:
    """Every decoded layer of *text*, each mapped to *text*: the layers of the
    full decode, then those of the chains that keep value delimiters as written
    (:data:`_CHAINS`), without repeats.

    A rule must read EVERY layer, not only the last: a further round can turn
    an escaped quote inside a value into a closing delimiter (a password with a
    ``"`` in JSON inside JSON), so the last layer cuts the value short where an
    earlier one reads it whole; and a label written with encoded quotes is only
    found where they are decoded. Empty when normalising changes nothing.
    """
    if len(text) <= _VIEWS_MEMO_TEXT_MAX:
        with _VIEWS_MEMO_LOCK:
            cached = _VIEWS_MEMO.get(text)
        if cached is not None:
            return list(cached)
    views: list[View] = []
    seen: set[str] = set()
    for tokens in _CHAINS:
        for view in _decode_chain(text, tokens):
            if view.text not in seen:
                seen.add(view.text)
                views.append(view)
    if len(text) <= _VIEWS_MEMO_TEXT_MAX:
        # The stream asks for one held buffer's views from several rules per
        # chunk; a few recent buffers, each bounded in size, are kept.
        # Several threads redact at once (an archive read in the executor, the
        # stream on the event loop): the check, the eviction and the insert are
        # one step under the lock, or two threads evict the same key.
        with _VIEWS_MEMO_LOCK:
            if text not in _VIEWS_MEMO and len(_VIEWS_MEMO) >= _VIEWS_MEMO_SIZE:
                _VIEWS_MEMO.pop(next(iter(_VIEWS_MEMO)), None)
            _VIEWS_MEMO[text] = tuple(views)
    return views


#: The last few texts' views. Bounded in count and in text size, so neither a
#: finished stream nor a large event is retained.
_VIEWS_MEMO: dict[str, tuple[View, ...]] = {}
_VIEWS_MEMO_SIZE = 4
_VIEWS_MEMO_LOCK = threading.Lock()
_VIEWS_MEMO_TEXT_MAX = 8 * 1024


def normalised_view(text: str) -> View | None:
    """The last layer of the full decode, or None when it changes nothing."""
    layers = _decode_chain(text, _TOKEN_RE)
    return layers[-1] if layers else None


_SPACE_RE = re.compile(r"\s")


def _token_edge_before(text: str, pos: int) -> int:
    """The start of the whitespace-free run that holds *pos*."""
    pos = max(0, pos)
    while pos > 0 and not text[pos - 1].isspace():
        pos -= 1
    return pos


def _token_edge_after(text: str, pos: int) -> int:
    """The end of the whitespace-free run that holds *pos*."""
    if pos >= len(text) or text[pos].isspace():
        return min(len(text), pos)
    space = _SPACE_RE.search(text, pos)
    return space.start() if space is not None else len(text)


#: How far back a window looks for the header of a PEM block it starts inside.
_PEM_BLOCK_REACH = 64 * 1024


def _pem_block_edges(text: str, lo: int, hi: int) -> tuple[int, int]:
    """*lo* and *hi* moved out to the PEM block either edge falls inside: a
    window that starts in a key's body reads its header, and one that ends in
    it reads through its footer, so the key is matched whole in the view. Only
    a block whose footer is in reach is widened to: an unclosed header would
    pull every later window back to it and re-read the same text."""
    floor = max(0, lo - _PEM_BLOCK_REACH)
    begin = text.rfind("-----BEGIN", floor, lo)
    if begin != -1 and text.find("-----END", begin, lo) == -1:
        if text.find("-----END", lo, begin + _PEM_BLOCK_REACH) != -1:
            lo = text.rfind("\n", 0, begin) + 1
    begin = text.rfind("-----BEGIN", max(lo, hi - _PEM_BLOCK_REACH), hi)
    if begin != -1 and text.find("-----END", begin, hi) == -1:
        end = text.find("-----END", hi, begin + _PEM_BLOCK_REACH)
        if end != -1:
            line_end = text.find("\n", end)
            hi = len(text) if line_end == -1 else line_end
    return lo, hi


def window_edges(text: str, lo: int, hi: int) -> tuple[int, int]:
    """``text[lo:hi]`` widened so a view of it never cuts a token or a PEM
    block: to whitespace on both sides, then out to the block either edge
    falls inside. A view read through it finds a match whose identifying
    prefix lies outside the bare window."""
    lo, hi = _token_edge_before(text, lo), _token_edge_after(text, hi)
    return _pem_block_edges(text, lo, hi)


def changed_windows(text: str, pad: int) -> list[tuple[int, int]]:
    """The source ranges normalising can change, each widened by *pad* a side.

    A rule run over the view can only find something new near a character the
    view changes, within that rule's own reach; *pad* is the largest reach. The
    windows are merged and sorted, and cost one scan of *text*.
    """
    windows: list[tuple[int, int]] = []
    for m in _TOKEN_RE.finditer(text):
        lo, hi = max(0, m.start() - pad), min(len(text), m.end() + pad)
        if windows and lo <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
        else:
            windows.append((lo, hi))
    return windows

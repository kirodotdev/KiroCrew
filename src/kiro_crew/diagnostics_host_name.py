"""Replace this machine's host names in a diagnostics bundle member with ``<host>``.

A crash-dump header and the ``last-healthy-boot`` marker record a PID domain that
starts with the host name, and a work machine's name often carries its user's name
or an asset tag, so every text member of the bundle is passed through
:func:`replace_host_names` before it is written.

The matcher reads two copies of the text:

- the text as written, and
- a decoded view: a run of ``%XX`` or ``\\xXX`` bytes reads as the UTF-8 text it
  encodes and a run of ``\\uXXXX`` units as the UTF-16 text it encodes, one view
  character per encoded character, a serialized escape such as ``\\n`` reads as one
  non-name character, and ``\\\\`` reads as one backslash. Every view character
  keeps the span of the original characters it came from, so a match in the view
  replaces exactly those characters.

A host name is a run of name characters (ASCII letters and digits, ``-``, ``_``).
A match ends where the next character is not one, so ``jane-laptop2`` is another
machine while ``jane-laptop.lan`` still matches. A name of
:data:`HOST_ANYWHERE_MIN` characters or more is replaced wherever it starts; a
shorter one only where it starts a segment, so a generic short name does not
rewrite ordinary words.

A short name starts a segment after a non-name character, after an ANSI control
sequence (``[32m``), after a literal ``\\`` when the name has two characters or
more (a Windows path separator; a one-letter name would take the letter of a
serialized ``\\t``), and where a hidden character the credential scrub removes
joined it onto the word in front (:func:`find_joined_host_words`).
"""

from __future__ import annotations

import bisect
import re
import socket
import unicodedata
from dataclasses import dataclass

from kiro_crew.dashboard import crash_dump_store

#: A host name this long is replaced anywhere it appears; a shorter one only where
#: it starts a segment.
HOST_ANYWHERE_MIN = 6

_NAME_CHARS = "A-Za-z0-9_-"
_HOST_END = rf"(?![{_NAME_CHARS}])"
_NAME_CHAR = re.compile(rf"[{_NAME_CHARS}]")
#: An encoded run of the decoded view. ``\\\\`` comes first so the ``u`` of an
#: escaped backslash followed by ``u002d`` is read as a letter. A run of encoded
#: units is decoded as one sequence (:func:`_decode_token`).
_ENCODED = re.compile(
    r"\\\\|(?:\\u[0-9A-Fa-f]{4})+|(?:\\x[0-9A-Fa-f]{2})+|(?:%[0-9A-Fa-f]{2})+|\\[A-Za-z]"
)
#: How many characters of the word in front of a joined name are kept to find the
#: join again after the scrub. A bound keeps the lookup per occurrence constant.
_JOIN_CONTEXT = 16
#: Stands for a serialized escape in the view: a non-name character.
_ESCAPE_STAND_IN = "\x00"


def host_names() -> list[str]:
    """This machine's host names, longest first.

    The live name and the one the crash-dump writer cached at boot, each in full and
    in its short form before the first ``.``, plus the other IDNA spelling of each
    (``xn--`` for a Unicode name, Unicode for an ``xn--`` one).
    """
    own = crash_dump_store.current_process_identity()[0].partition("/")[0]
    names: set[str] = set()
    for full in (socket.gethostname(), own):
        for name in (full, full.partition(".")[0]):
            if name:
                names.add(name)
                names.update(_idna_spellings(name))
    return sorted(names, key=lambda n: (-len(n), n))


def _idna_spellings(name: str) -> set[str]:
    """The other IDNA spelling of *name*, when it has one that the codec accepts."""
    try:
        if not name.isascii():
            return {name.encode("idna").decode("ascii")}
        if "xn--" in name.lower():
            # The codec decodes only a lower-case ``xn--`` label.
            return {name.lower().encode("ascii").decode("idna")}
    except UnicodeError:
        pass
    return set()


@dataclass(frozen=True)
class JoinedHostWord:
    """A short host name a stripped hidden character joined onto *prefix*."""

    prefix: str
    name: str


def find_joined_host_words(raw: str, names: list[str]) -> set[JoinedHostWord]:
    """Short names in *raw* that the hidden-character strip would join onto a word.

    The credential scrub removes hidden characters before it redacts, so a short
    name behind a form feed (``boot<FF>box``) reaches the host pass as ``bootbox``
    with no boundary left. This reads the decoded view of *raw* before that strip,
    so an encoded name (``boot<FF>%62ox``) is found too, and records each joined
    word, so :func:`replace_host_names` can replace the name inside it.
    """
    short = [n for n in names if len(n) < HOST_ANYWHERE_MIN]
    if not short:
        return set()
    pattern = re.compile(
        rf"(?<![{_NAME_CHARS}])([{_NAME_CHARS}]+)({_MAYBE_HIDDEN}+)"
        rf"(?=((?:{_alternation(short)}){_HOST_END}))",
        re.IGNORECASE,
    )
    found: set[JoinedHostWord] = set()
    for m in pattern.finditer(_View(raw).text):
        if all(_is_stripped(ch) for ch in m.group(2)):
            prefix = m.group(1)[-_JOIN_CONTEXT:].lower()
            found.add(JoinedHostWord(prefix, m.group(3).lower()))
    return found


#: A character the hidden-character strip may remove: an ASCII control other than
#: ``\\n``, ``\\r`` and ``\\t``, or any non-ASCII character (:func:`_is_stripped`
#: decides).
_MAYBE_HIDDEN = r"(?:[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|[^\x00-\x7f])"


def _is_stripped(ch: str) -> bool:
    """Whether the hidden-character strip removes *ch* (control, format, surrogate)."""
    return ch not in "\n\r\t" and unicodedata.category(ch) in ("Cc", "Cf", "Cs")


class _View:
    """The decoded view of a text, with each view character's original span."""

    def __init__(self, text: str) -> None:
        pieces: list[str] = []
        self._token_view: list[int] = []
        self._token_orig: list[int] = []
        self._token_len: list[int] = []
        last = 0
        view_len = 0
        for m in _ENCODED.finditer(text):
            pieces.append(text[last : m.start()])
            view_len += m.start() - last
            token = m.group()
            decoded = _decode_token(token)
            offset = m.start()
            for ch, length in decoded:
                self._token_view.append(view_len)
                self._token_orig.append(offset)
                self._token_len.append(length)
                pieces.append(ch)
                view_len += 1
                offset += length
            last = m.end()
        pieces.append(text[last:])
        self.text = "".join(pieces)

    def start(self, v: int) -> int:
        """Original offset of view character *v*."""
        i = bisect.bisect_right(self._token_view, v) - 1
        if i < 0:
            return v
        if self._token_view[i] == v:
            return self._token_orig[i]
        return self._token_orig[i] + self._token_len[i] + (v - self._token_view[i] - 1)

    def end(self, v: int) -> int:
        """Original offset just past view character *v*."""
        i = bisect.bisect_right(self._token_view, v) - 1
        if i >= 0 and self._token_view[i] == v:
            return self._token_orig[i] + self._token_len[i]
        return self.start(v) + 1


def _decode_token(token: str) -> list[tuple[str, int]]:
    """The view characters an :data:`_ENCODED` token stands for, each with the
    length of the original characters it came from.

    A run of encoded units is decoded by its codec as one sequence: ``%XX`` and
    ``\\xXX`` are UTF-8 bytes (``%C3%BC`` is one ``ü``), ``\\uXXXX`` are UTF-16 code
    units (``\\ud83d\\ude00`` is one emoji). A unit that starts no valid sequence
    reads as its own code point, or as a stand-in when that is a lone surrogate.
    """
    if token == "\\\\":
        return [("\\", 2)]
    if len(token) == 2:
        return [(_ESCAPE_STAND_IN, 2)]
    if token[0] == "%":
        unit, codec = 3, "utf-8"
    else:
        unit, codec = (4, "utf-8") if token[1] == "x" else (6, "utf-16-be")
    values = [
        int(token[i + 2 if unit > 3 else i + 1 : i + unit], 16) for i in range(0, len(token), unit)
    ]
    out: list[tuple[str, int]] = []
    i = 0
    while i < len(values):
        size = _sequence_size(values, i, codec)
        ch = _decode_units(values[i : i + size], codec) if size > 1 else ""
        if len(ch) != 1:
            size = 1
            ch = chr(values[i])
            if "\ud800" <= ch <= "\udfff":
                ch = _ESCAPE_STAND_IN
        out.append((ch, size * unit))
        i += size
    return out


def _sequence_size(values: list[int], i: int, codec: str) -> int:
    """How many units the sequence starting at ``values[i]`` claims in *codec*."""
    lead = values[i]
    if codec == "utf-16-be":
        has_low = i + 1 < len(values) and 0xDC00 <= values[i + 1] <= 0xDFFF
        return 2 if 0xD800 <= lead <= 0xDBFF and has_low else 1
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    return 4 if 0xF0 <= lead <= 0xF4 else 1


def _decode_units(units: list[int], codec: str) -> str:
    """*units* decoded by *codec*, or ``""`` when they are not one valid sequence."""
    width = 2 if codec == "utf-16-be" else 1
    try:
        return b"".join(u.to_bytes(width, "big") for u in units).decode(codec)
    except UnicodeDecodeError:
        return ""


def _starts_segment(text: str, at: int) -> bool:
    """Whether a short name starting at *at* in *text* starts a segment."""
    if at == 0 or not _NAME_CHAR.match(text[at - 1]):
        return True
    return _ends_control_sequence(text, at)


def _ends_control_sequence(text: str, at: int) -> bool:
    """Whether an ANSI control sequence (``[`` + parameters + a letter) ends at *at*.

    Its ESC is stripped or serialized before this runs, so the sequence is read from
    its ``[``, walking back over the parameter bytes with no length limit.
    """
    if not text[at - 1].isascii() or not text[at - 1].isalpha():
        return False
    i = at - 2
    while i >= 0 and (text[i].isdigit() and text[i].isascii() or text[i] in ";?"):
        i -= 1
    return i >= 0 and text[i] == "["


def replace_host_names(
    text: str,
    names: list[str],
    joined: set[JoinedHostWord] | frozenset[JoinedHostWord] = frozenset(),
) -> tuple[str, int]:
    """Replace every spelling of *names* in *text* with ``<host>``; return (text, count).

    *names* comes from :func:`host_names`, *joined* from :func:`find_joined_host_words`
    on the same member before it was scrubbed.
    """
    if not names or not text:
        return text, 0
    long_names = [n for n in names if len(n) >= HOST_ANYWHERE_MIN]
    short_names = [n for n in names if len(n) < HOST_ANYWHERE_MIN]
    view = _View(text)
    spans: list[tuple[int, int]] = []

    def in_view(m_start: int, m_end: int) -> None:
        spans.append((view.start(m_start), view.end(m_end - 1)))

    if long_names:
        anywhere = re.compile(rf"(?:{_alternation(long_names)}){_HOST_END}", re.IGNORECASE)
        spans.extend(m.span() for m in anywhere.finditer(text))
        for m in anywhere.finditer(view.text):
            in_view(*m.span())
    if short_names:
        each = re.compile(rf"(?=((?:{_alternation(short_names)}){_HOST_END}))", re.IGNORECASE)
        for m in each.finditer(view.text):
            if _starts_segment(view.text, m.start(1)):
                in_view(*m.span(1))
        after_backslash = [n for n in short_names if len(n) >= 2]
        if after_backslash:
            windows = re.compile(
                rf"(?<=\\)(?:{_alternation(after_backslash)}){_HOST_END}", re.IGNORECASE
            )
            spans.extend(
                m.span() for m in windows.finditer(text) if _in_windows_path(text, *m.span())
            )
    if joined:
        for start, end in _joined_occurrences(view.text, joined):
            in_view(start, end)
    return _apply(text, spans)


def _joined_occurrences(
    text: str, joined: set[JoinedHostWord] | frozenset[JoinedHostWord]
) -> list[tuple[int, int]]:
    """Spans in *text* of each joined name right after its recorded prefix.

    One pass over the name's occurrences, each checked against the recorded
    prefixes by their lengths (at most :data:`_JOIN_CONTEXT` of them), so the cost
    does not grow with the number of joins.
    No end boundary is required: the strip that joined the name onto its prefix may
    have joined it onto the next word too (``boot<FF>box<FF>box``).
    """
    by_name: dict[str, set[str]] = {}
    for word in joined:
        by_name.setdefault(word.name.casefold(), set()).add(word.prefix)
    lengths = sorted({len(w.prefix) for w in joined})
    names = sorted({word.name for word in joined})
    each = re.compile(rf"(?=({_alternation(names)}))", re.IGNORECASE)
    found: list[tuple[int, int]] = []
    for m in each.finditer(text):
        start, end = m.span(1)
        # A Unicode case variant the pattern matches (``İ``, ``ı``, ``ſ``) can fold
        # to no recorded name; it is no joined occurrence, so it is skipped.
        prefixes = by_name.get(m.group(1).casefold())
        if prefixes is None:
            continue
        before = text[max(0, start - lengths[-1]) : start]
        if not before.isascii():
            before = "".join(ch if ch.isascii() else "\x00" for ch in before)
        before = before.lower()
        if any(before[-n:] in prefixes for n in lengths if n <= len(before)):
            found.append((start, end))
    return found


#: How far back the run a path sits in is read: the classic ``MAX_PATH``.
_PATH_LOOKBACK = 260
#: Letters that make a serialized escape after a ``\\`` (``\\n``, ``\\t``, ``\\u``).
_ESCAPE_LETTERS = frozenset("abefnrtuvx")
#: Characters that end the run a path sits in: the ones a Windows path cannot hold,
#: a line break, and a quote. A space, ``(``, ``;`` or ``=`` can sit inside a path
#: (``D:\\Project Files\\box``), so they do not end it.
_PATH_DELIMITERS = "\t\r\n\"'<>|"
#: Evidence that a run is a Windows path: a drive that is not the end of a word (so
#: the ``r:`` of ``error:\\n`` is no drive), a UNC ``\\\\``, a collapsed home or
#: data home ``~\\``, or a ``\\`` before anything but an escape letter
#: (``\\Users``, ``\\Desktop``, ``\\2026``).
_PATH_HINT = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:\\|\\\\|(?<![^\s\"'=(\[])~\\|\\(?![abefnrtuvx\\])[^\s\"']"
)


def _in_windows_path(text: str, start: int, end: int) -> bool:
    """Whether the name at ``text[start:end]``, right after a ``\\``, is a segment
    of a Windows path rather than the letter of a serialized escape.

    A name whose first letter makes no escape after a ``\\`` always is. Otherwise
    it is when a path separator follows it (``C:\\Users\\box\\logs``), or when the
    run before it, which a quote, a line break or a character no path can hold
    ends, shows a path (:data:`_PATH_HINT`): ``~\\Desktop\\box.log``,
    ``D:\\Project Files\\box``, ``\\\\box``. ``done\\nAs expected`` shows none, so
    its ``\\n`` stays whole.
    """
    if text[start].lower() not in _ESCAPE_LETTERS:
        return True
    if end < len(text) and text[end] in "\\/":
        return True
    window = text[max(0, start - _PATH_LOOKBACK) : start]
    run_start = max(window.rfind(ch) for ch in _PATH_DELIMITERS) + 1
    return bool(_PATH_HINT.search(window, run_start))


def _alternation(names: list[str]) -> str:
    """A regex alternation of *names*, longest first so a full name wins."""
    return "|".join(map(re.escape, sorted(names, key=len, reverse=True)))


def _apply(text: str, spans: list[tuple[int, int]]) -> tuple[str, int]:
    """Replace the union of *spans* in *text* with ``<host>``, one per merged span."""
    if not spans:
        return text, 0
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out: list[str] = []
    last = 0
    for start, end in merged:
        out.append(text[last:start])
        out.append("<host>")
        last = end
    out.append(text[last:])
    return "".join(out), len(merged)

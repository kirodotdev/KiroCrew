"""Detector E: an encoded run whose decoded text holds a credential another row reads.

Two encodings are read: base64 and percent-encoding (``%67%68%70%5F...``, the form
a URL query carries). The text is written one byte per character (Latin-1, every
other character as ``?``), mapped through a byte table that blanks each byte outside
the encoding's alphabet, and split at the blanks with ``bytes.split``, so the scan
runs at C speed and only a run long enough to decode costs a Python step; for
base64 a ``=`` ends a run (``key=value`` splits there) and padding is restored on
decode. A run of at least the row's ``min_length`` is decoded (a percent run needs
at least one escape); a decode that fails, or whose first bytes are not text, is
skipped at once. The decoded text is read by the whole table again (one level
deeper, so a credential encoded twice is found), and the run is ONE Secret hit when
that read finds a Secret. A random digest, an image payload, a certificate body or
an encoded path decodes to no credential and stays. A run is cut at every non-ASCII
character too, and its head is decoded first: only a run whose head reads as text
is decoded whole, while :data:`DECODE_BUDGET` lasts; past it such a run is withheld
(one hit) rather than passed unread. A stream window that ends inside a run holds it
while the whole text may still mask it, and withholds it past :data:`TEXT_HOLD_MAX`,
so no part of a run the whole text masks is committed as written.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterator

from .charsets import ALNUM
from .model import TAIL_ENCRUN, Hit
from .table import EncodedRow

#: Deepest decode: a credential encoded twice is still read.
MAX_DEPTH = 2
#: Decoded bytes one scan may spend, so a page of long runs costs a bounded amount.
DECODE_BUDGET = 256 * 1024
#: The longest run a stream holds while it may still grow, whatever it reads as so far.
#: A longer run is held only while the whole text may still mask it (:func:`_hold`); one
#: it cannot (an image, a digest, a run with a malformed escape) streams on without
#: waiting.
HOLD_MAX = 448
#: The longest run a stream holds while the whole text may still mask it: its head is
#: text, so its decode may still end in a credential. Past it the run is withheld to its
#: end (one hit), as a run read past :data:`DECODE_BUDGET` is.
TEXT_HOLD_MAX = 4096
#: How many leading decoded bytes the text check reads, and how many must be text.
_HEAD = 64
#: The encoded characters that hold those bytes: a run is decoded whole only when
#: its head reads as text.
_HEAD_CHARS = 88
_TEXT_SHARE = 0.9
_ALPHABET = ALNUM + "+/-_"
_TEXT_BYTES = frozenset(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}
#: Every byte that is not text: deleting them from a head leaves its text bytes.
_NON_TEXT = bytes(b for b in range(256) if b not in _TEXT_BYTES)
#: A percent-encoded value: the URL unreserved characters, ``+`` and the escapes.
_PERCENT_ALPHABET = ALNUM + "%-._~+"
#: Every character either kind of run is read in: a stream withholding a run drops these.
RUN_CHARS = "".join(dict.fromkeys(_ALPHABET + _PERCENT_ALPHABET))


def _split_table(alphabet: str) -> bytes:
    """A byte table keeping each (ASCII) byte of *alphabet* and blanking every other.

    Every non-ASCII character is blanked too: no encoding here holds one."""
    return bytes(b if chr(b) in alphabet else 0 for b in range(256))


_SPLIT = _split_table(_ALPHABET)
_PERCENT_SPLIT = _split_table(_PERCENT_ALPHABET)
#: Escapes a run needs before it is read as percent-encoded text: one is enough, since
#: an encoded ``=`` alone hides a label's value (``aws_secret_access_key%3D...``).
_PERCENT_MIN_ESCAPES = 1
_HEX = frozenset("0123456789abcdefABCDEF")

Reader = Callable[[str, int], bool]


def _decoded(run: str, limit: int | None = None) -> str | None:
    """*run* decoded as base64 (url-safe when it holds ``-`` or ``_``), when it is text.

    With *limit*, only its first *limit* characters (rounded down to whole quanta)
    are decoded: enough to tell text from an image or a digest without paying for
    the whole run."""
    body = run if limit is None or len(run) <= limit else run[: limit - limit % 4]
    padded = body + "=" * (-len(body) % 4)
    try:
        if "-" in run or "_" in run:
            raw = base64.urlsafe_b64decode(padded)
        else:
            raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    return _text(raw)


def _text(raw: bytes | bytearray) -> str | None:
    head = raw[:_HEAD]
    if not head or len(head.translate(None, _NON_TEXT)) < _TEXT_SHARE * len(head):
        return None
    return bytes(raw).decode("utf-8", "replace")


def _percent_decoded(run: str) -> str | None:
    """*run* with its ``%XX`` escapes decoded, when it holds enough of them and is text."""
    parts = run.split("%")
    if len(parts) - 1 < _PERCENT_MIN_ESCAPES:
        return None
    raw = bytearray(parts[0].encode())
    for part in parts[1:]:
        if len(part) < 2 or part[0] not in _HEX or part[1] not in _HEX:
            return None
        raw.append(int(part[:2], 16))
        raw += part[2:].encode()
    return _text(raw)


def _text_head(run: str) -> bool:
    """Whether *run*'s head decodes as base64 text: only then can its decode hold a credential."""
    return _decoded(run, _HEAD_CHARS) is not None


def _percent_may_decode(run: str) -> bool:
    """Whether a percent run a window ends inside may still decode as text once it ends.

    An escape the window's end cuts off may still complete; one malformed escape, or a
    head that is not text, settles it.
    """
    cut = run.find("%", len(run) - 2)
    body = run if cut == -1 else run[:cut]
    return "%" not in body or _percent_decoded(body) is not None


def _hold(
    start: int,
    run: str,
    may_mask: Callable[[str], bool],
    row: EncodedRow,
    hits: list[Hit],
    opens: list[int],
) -> None:
    """Hold a run a stream window ends inside while the whole text may still mask it.

    Past :data:`TEXT_HOLD_MAX` the run is withheld instead: one hit to the window's end,
    which the stream replaces and then drops the rest of the run.
    """
    if len(run) <= HOLD_MAX:
        opens.append(start)
    elif may_mask(run):
        if len(run) <= TEXT_HOLD_MAX:
            opens.append(start)
        else:
            end = start + len(run)
            hits.append(
                Hit(start, end, row.id, row.tier, "encoded", "fixed_format", tail=TAIL_ENCRUN)
            )


def _runs(text: str, split: bytes, min_length: int) -> Iterator[tuple[int, str]]:
    """Every run of at least *min_length* characters *split* keeps, with its start.

    One byte per character, so a byte offset is a character offset; a non-Latin-1
    character is written as ``?``, which no alphabet here holds."""
    pos = 0
    for run in text.encode("latin-1", "replace").translate(split).split(b"\0"):
        size = len(run)
        if size >= min_length:
            yield pos, text[pos : pos + size]
        pos += size + 1


def detect_encoded(
    text: str,
    rows: tuple[EncodedRow, ...],
    final: bool,
    depth: int,
    holds_secret: Reader,
    origin: int = 0,
) -> tuple[list[Hit], list[int]]:
    """``(hits, opens)``; *holds_secret(decoded, depth)* reads a decode with the table.

    *origin* is where a stream window's text not committed yet starts (0 for a whole text).
    """
    if not rows or depth >= MAX_DEPTH:
        return [], []
    row = rows[0]
    hits: list[Hit] = []
    opens: list[int] = []
    budget = DECODE_BUDGET
    n = len(text)
    for start, run in _runs(text, _SPLIT, row.min_length):
        end = start + len(run)
        if end == n and not final:
            _hold(start, run, _text_head, row, hits, opens)
            continue
        if not _text_head(run):
            continue  # not text (an image, a digest): nothing a credential could be in
        # The run's ``=`` padding goes with a hit on it (at most two).
        stop = end
        while stop < n and stop - end < 2 and text[stop] == "=":
            stop += 1
        if budget <= 0:
            # Text no decode is left to read: withheld, never passed as clean.
            hits.append(Hit(start, stop, row.id, row.tier, "encoded", "fixed_format"))
            continue
        budget -= len(run)
        decoded = _decoded(run)
        if decoded is not None and holds_secret(decoded, depth + 1):
            hits.append(Hit(start, stop, row.id, row.tier, "encoded", "fixed_format"))
    if "%" not in text:
        return hits, opens
    for start, run in _runs(text, _PERCENT_SPLIT, row.min_length):
        if "%" not in run:
            continue
        end = start + len(run)
        if start < origin < end:
            # A stream committed the head of this run while it held no escape (a run
            # with one is held). The whole text decodes that head with the escapes
            # after it, so the rest of the run is withheld rather than committed.
            if "%" in text[origin:end] and _percent_may_decode(run):
                tail = TAIL_ENCRUN if end == n and not final else ""
                hits.append(
                    Hit(origin, end, row.id, row.tier, "encoded", "fixed_format", tail=tail)
                )
            continue
        if end == n and not final:
            _hold(start, run, _percent_may_decode, row, hits, opens)
            continue
        decoded = _percent_decoded(run)
        if decoded is None:
            continue
        if budget <= 0:
            hits.append(Hit(start, end, row.id, row.tier, "encoded", "fixed_format"))
            continue
        budget -= len(run)
        if holds_secret(decoded, depth + 1):
            hits.append(Hit(start, end, row.id, row.tier, "encoded", "fixed_format"))
    return hits, opens

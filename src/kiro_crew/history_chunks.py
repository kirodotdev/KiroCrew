"""Cut session messages into chunks of at most :data:`CHUNK_TOKENS` model tokens.

A chunk never spans two messages, so every chunk addresses one message by its
index and a character range inside it. The token bound is measured with the
embedding model's own tokenizer, passed in as *count_tokens*, so one chunk costs
one model input whatever the script: a Latin and a CJK passage of the same length
differ about twofold in tokens.

A lone UTF-16 surrogate (a JSON ``\\udXXX`` escape with no partner) is replaced
with U+FFFD before cutting, as ``history_index.fold_texts`` does: SQLite cannot
store it, and one code point replaces one, so offsets still address the message.

The bound is an upper bound. A chunk is grown over whole units (a word with its
trailing space, or one CJK character) and the largest prefix that fits is found by
bisection, so a chunk may stop short of the bound but never exceeds it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from typing import NamedTuple

#: The token bound per chunk (RFC ``rfc-session-semantic-search`` section 7.1).
CHUNK_TOKENS = 300

#: A unit longer than this is split, so one unit is always far below the bound:
#: a character costs at most four tokens in a byte-level vocabulary.
_MAX_UNIT_CHARS = 64

#: A chunk is never probed past this many characters per allowed token. Text that
#: dense in characters per token ends the chunk early, which the bound permits.
_MAX_CHARS_PER_TOKEN = 12

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af"
_UNIT = re.compile(rf"[{_CJK}]\s*|[^\s{_CJK}]+\s*|\s+")


class Chunk(NamedTuple):
    message_index: int
    char_start: int
    char_end: int
    text: str
    text_hash: bytes
    token_count: int


def chunk_messages(
    texts: Sequence[str],
    count_tokens: Callable[[str], int],
    *,
    max_tokens: int = CHUNK_TOKENS,
) -> list[Chunk]:
    """Chunks of every message in *texts*, in order. Whitespace-only text yields none.

    A chunk's ``message_index`` is the position of its message in *texts*. The
    chunk pass passes ``_iter_message_texts``, so that is the reader that resolves it.
    """
    chunks: list[Chunk] = []
    for index, text in enumerate(texts):
        clean = _LONE_SURROGATE.sub("\ufffd", text)
        chunks.extend(_chunk_one(index, clean, count_tokens, max_tokens))
    return chunks


def _units(text: str) -> list[int]:
    """End offsets of *text*'s units, each at most :data:`_MAX_UNIT_CHARS` long."""
    ends: list[int] = []
    for match in _UNIT.finditer(text):
        start, end = match.span()
        while end - start > _MAX_UNIT_CHARS:
            start += _MAX_UNIT_CHARS
            ends.append(start)
        ends.append(end)
    return ends


def _chunk_one(
    index: int, text: str, count_tokens: Callable[[str], int], max_tokens: int
) -> list[Chunk]:
    ends = _units(text)
    chunks: list[Chunk] = []
    start, first = 0, 0
    while first < len(ends):
        char_cap = start + max_tokens * _MAX_CHARS_PER_TOKEN
        last = first
        while last + 1 < len(ends) and ends[last + 1] <= char_cap:
            last += 1
        # Largest unit index in [first, last] whose prefix fits. ``first`` is kept
        # even when it alone does not fit, so the loop always advances; at the
        # default bound one unit always fits (see _MAX_UNIT_CHARS).
        lo, hi = first, last
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count_tokens(text[start : ends[mid]]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        end = ends[lo]
        piece = text[start:end]
        if piece.strip():
            chunks.append(
                Chunk(
                    message_index=index,
                    char_start=start,
                    char_end=end,
                    text=piece,
                    text_hash=hashlib.sha256(piece.encode("utf-8")).digest(),
                    token_count=count_tokens(piece),
                )
            )
        start, first = end, lo + 1
    return chunks

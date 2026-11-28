"""Which characters belong to a script written without word spacing (CJK, kana).

Pure text helpers, split from ``_sqlite_compat`` so a reader that only needs them
does not load a SQLite driver.
"""

from __future__ import annotations

import re

# Scripts that write words with no space between them, so whitespace carries no
# word boundary and `query.split()` yields one token per *phrase* rather than per
# word. Mirrors `history_search._is_cjk_char`, the character set the session
# search's CJK gate already ships; Hangul is deliberately absent from both,
# because modern Korean is space-separated and needs no segmentation.
_KANA_RANGES = (
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x31F0, 0x31FF),  # Katakana Phonetic Extensions
)
_CJK_RANGES = (
    *_KANA_RANGES,
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x2EBEF),  # CJK Unified Ideographs Extensions B..F (astral)
)
_CJK_MIN = min(lo for lo, _ in _CJK_RANGES)


def is_cjk_char(ch: str) -> bool:
    """True if ``ch`` belongs to a script written without word spacing."""
    cp = ord(ch)
    if cp < _CJK_MIN:  # ASCII and Latin text never reach the range scan
        return False
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def is_kana_char(ch: str) -> bool:
    """True if ``ch`` is Japanese kana: the part of the CJK set that is not Han.

    Session search splits a pure-Han run into Chinese dictionary words; a run
    holding any kana is Japanese and keeps the character path.
    """
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _KANA_RANGES)


# Derived from _CJK_RANGES rather than spelled out, so the two cannot drift apart.
_CJK_PATTERN = re.compile("([" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _CJK_RANGES) + "])")


def script_runs(text: str) -> list[tuple[str, bool]]:
    """Split ``text`` into maximal (chunk, is_cjk) runs, preserving order.

    Public because session search needs the same split: keeping a second copy
    there is what let the CJK character ranges drift apart in the first place.
    Returns a list rather than a generator so the result can be re-iterated, and
    tolerates an empty string instead of raising.
    """
    runs: list[tuple[str, bool]] = []
    current = ""
    flag: bool | None = None
    for ch in text:
        cjk = is_cjk_char(ch)
        if flag is not None and cjk != flag:
            runs.append((current, bool(flag)))
            current = ""
        current += ch
        flag = cjk
    if current:
        runs.append((current, bool(flag)))
    return runs

"""Where a text may be cut without splitting one visible character in two.

A cutter that counts code points can land inside a grapheme cluster: between a
flag's two regional indicators, inside a ZWJ emoji, or before a mark that belongs
to the character before it. Both halves then show broken glyphs. Every cutter
asks :func:`joins_previous` before it cuts, so a rule added here reaches all of
them: the shared chat splitter, iMessage's plain-text rendering, outbound
notifications, chat titles and computer-use output.

Channel-neutral on purpose, so ``messaging`` can use it without importing a
channel package.
"""

from __future__ import annotations

import unicodedata

#: Characters that must never be separated from what precedes them.
_ZWJ = "\u200d"
_VARIATION_SELECTORS = range(0xFE00, 0xFE10)
_VARIATION_SUPPLEMENT = range(0xE0100, 0xE01F0)
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_REGIONAL_INDICATORS = range(0x1F1E6, 0x1F200)
_KEYCAP = 0x20E3
_TAG_RANGE = range(0xE0020, 0xE0080)

#: General categories that carry Grapheme_Extend. ``unicodedata.combining``
#: reports the canonical combining CLASS, which is 0 for a large share of real
#: marks -- every Thai vowel sign above the baseline (U+0E31 MAO EK, U+0E34,
#: U+0E47), Khmer and the Indic matras -- so testing the class alone would cut
#: after those marks and split a grapheme the reader sees as one character. The
#: CATEGORY is the property that holds: a mark belongs to the preceding base
#: whatever its class. Erring wide can only move a cut earlier, never split a
#: cluster, so it keeps the guarantee ("never separate a mark from its base").
_MARK_CATEGORIES = frozenset({"Mn", "Mc", "Me"})


def joins_previous(text: str, index: int) -> bool:
    """True when cutting at ``index`` would break a grapheme cluster."""
    following = text[index]
    code = ord(following)
    if (
        following == _ZWJ
        or unicodedata.combining(following)
        or unicodedata.category(following) in _MARK_CATEGORIES
        or code in _VARIATION_SELECTORS
        or code in _VARIATION_SUPPLEMENT
        or code in _SKIN_TONES
        or code in _TAG_RANGE
        or code == _KEYCAP
    ):
        return True
    previous = text[index - 1]
    if previous == _ZWJ:
        return True
    # A flag is an EVEN-length run of regional indicators. Cutting inside a
    # pair turns one flag into two stray letters, so only an even offset from
    # the run's start is a boundary.
    if code in _REGIONAL_INDICATORS and ord(previous) in _REGIONAL_INDICATORS:
        start = index - 1
        while start > 0 and ord(text[start - 1]) in _REGIONAL_INDICATORS:
            start -= 1
        return (index - start) % 2 == 1
    return False

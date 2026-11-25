"""Unicode's ``Default_Ignorable_Code_Point`` table: the code points drawn as nothing.

A leaf, imported by the artifact store's tag rules and by the credential redactor,
so both read one table pinned to one Unicode version.
"""

from __future__ import annotations

#: ``Default_Ignorable_Code_Point`` (Unicode 15.0, ``DerivedCoreProperties.txt``):
#: the code points that render as nothing. Most are format characters the
#: category test already refuses, but the property also holds letters and marks
#: -- the variation selectors (U+FE00-FE0F, U+E0100-E01EF, Mongolian U+180B-180D
#: and U+180F), the combining grapheme joiner, the Khmer inherent vowels and the
#: Hangul fillers -- which would pass as ``Mn`` or ``Lo``. Admitting one would
#: break "always visible, one spelling": ``ops`` and ``ops<VS16>`` would be two
#: stored tags with one look, and a key id with an invisible mark inside it would
#: pass a credential redactor while reading as the bare key. The table is the
#: whole published property, so it can be checked line by line against the
#: standard; ``unicodedata`` does not expose the property. It matches Python
#: 3.12's Unicode 15.0.0 and already covers the reserved code points in its
#: ranges. When the runtime's Unicode version moves, refresh it from
#: ``DerivedCoreProperties.txt``: on such a runtime
#: ``test_the_invisible_table_matches_the_runtime_unicode_version`` skips and
#: says so; it runs on CI's 3.12 runners.
DEFAULT_IGNORABLE_UNICODE_VERSION = "15.0.0"
DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),  # SOFT HYPHEN
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER
    (0x061C, 0x061C),  # ARABIC LETTER MARK
    (0x115F, 0x1160),  # HANGUL CHOSEONG FILLER..HANGUL JUNGSEONG FILLER
    (0x17B4, 0x17B5),  # KHMER VOWEL INHERENT AQ..KHMER VOWEL INHERENT AA
    (0x180B, 0x180D),  # MONGOLIAN FREE VARIATION SELECTOR ONE..THREE
    (0x180E, 0x180E),  # MONGOLIAN VOWEL SEPARATOR
    (0x180F, 0x180F),  # MONGOLIAN FREE VARIATION SELECTOR FOUR
    (0x200B, 0x200F),  # ZERO WIDTH SPACE..RIGHT-TO-LEFT MARK
    (0x202A, 0x202E),  # LEFT-TO-RIGHT EMBEDDING..RIGHT-TO-LEFT OVERRIDE
    (0x2060, 0x2064),  # WORD JOINER..INVISIBLE PLUS
    (0x2065, 0x2065),  # <reserved>
    (0x2066, 0x206F),  # LEFT-TO-RIGHT ISOLATE..NOMINAL DIGIT SHAPES
    (0x3164, 0x3164),  # HANGUL FILLER
    (0xFE00, 0xFE0F),  # VARIATION SELECTOR-1..VARIATION SELECTOR-16
    (0xFEFF, 0xFEFF),  # ZERO WIDTH NO-BREAK SPACE
    (0xFFA0, 0xFFA0),  # HALFWIDTH HANGUL FILLER
    (0xFFF0, 0xFFF8),  # <reserved>
    (0x1BCA0, 0x1BCA3),  # SHORTHAND FORMAT LETTER OVERLAP..SHORTHAND FORMAT UP STEP
    (0x1D173, 0x1D17A),  # MUSICAL SYMBOL BEGIN BEAM..MUSICAL SYMBOL END PHRASE
    (0xE0000, 0xE0000),  # <reserved>
    (0xE0001, 0xE0001),  # LANGUAGE TAG
    (0xE0002, 0xE001F),  # <reserved>
    (0xE0020, 0xE007F),  # TAG SPACE..CANCEL TAG
    (0xE0080, 0xE00FF),  # <reserved>
    (0xE0100, 0xE01EF),  # VARIATION SELECTOR-17..VARIATION SELECTOR-256
    (0xE01F0, 0xE0FFF),  # <reserved>
)
DEFAULT_IGNORABLE = frozenset(cp for lo, hi in DEFAULT_IGNORABLE_RANGES for cp in range(lo, hi + 1))

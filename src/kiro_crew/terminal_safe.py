"""Terminal-safe rendering for untrusted text.

This module is a stdlib-only leaf so CLI, doctor, and lightweight HTTP clients
can share one control-sequence policy without importing each other.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["normalize_for_scanning", "safe_terminal_line", "strip_control_characters"]

# Strip complete OSC and CSI sequences, other two-byte ESC sequences, and C0/C1
# controls while preserving newlines and tabs. OSC must precede the generic ESC
# alternative so its payload is removed with its introducer and terminator.
_TERMINAL_CTRL_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC through BEL or ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI with the full ECMA-48 parameter class
    r"|\x1b[ -/]*[@-~]"  # other two-byte ESC sequences
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"  # C0/C1 controls (keep \n and \t)
)

#: C0 and C1 controls and DEL, minus the three kept as content.
_SCAN_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: The invisible code points that ``unicodedata`` does NOT report as category ``Cf``.
#: Default-ignorable and rendering as nothing, so one of them splits a token exactly as a
#: zero-width space does, while a category test alone walks straight past it. Enumerated
#: because Python exposes no Default_Ignorable_Code_Point property to ask instead.
_INVISIBLE_NON_CF = frozenset(
    chr(code)
    for start, end in (
        (0x034F, 0x034F),  # combining grapheme joiner
        (0x115F, 0x1160),  # Hangul choseong and jungseong fillers
        (0x17B4, 0x17B5),  # Khmer inherent vowels
        (0x180B, 0x180D),  # Mongolian free variation selectors
        (0x180F, 0x180F),  # Mongolian free variation selector four
        (0x2065, 0x2065),  # unassigned, default ignorable
        (0x3164, 0x3164),  # Hangul filler
        (0xFE00, 0xFE0F),  # variation selectors 1 to 16
        (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
        (0xFFF0, 0xFFF8),  # unassigned, default ignorable
    )
    for code in range(start, end + 1)
)

#: Plane 14's tag block, default-ignorable from end to end and tested as a range rather
#: than enumerated: 4,096 contiguous code points is a lot of single-character strings to
#: hold for a membership test an integer comparison answers. Every category appears in it
#: -- the language tag and the tag characters are ``Cf``, variation selectors 17 to 256
#: are ``Mn``, and the four reserved stretches between them are ``Cn``, which no category
#: test above recognises as invisible. A renderer draws none of them, so any one splits a
#: token as effectively as a zero-width space.
_TAG_BLOCK = range(0xE0000, 0xE1000)


def _is_invisible(character: str) -> bool:
    """Whether ``character`` renders as nothing and so can split a token unseen."""
    return (
        unicodedata.category(character) == "Cf"
        or character in _INVISIBLE_NON_CF
        or ord(character) in _TAG_BLOCK
    )


_TERMINAL_TEXT_MAX = 2000


def strip_control_characters(value: str) -> str:
    """Return ``value`` with the C0 and C1 control characters removed.

    A control character is terminal-escape material, not text a user wrote, so a stored
    field carries it out to no one's benefit: it drives a terminal that renders the field
    verbatim, and it splits a token for any scanner that matches a pattern. Tab, newline
    and carriage return are kept, because those three ARE content.

    This is the half of :func:`normalize_for_scanning` a caller can apply to its OUTPUT.
    The other half removes format characters, which are usually content, so a caller that
    hands a stored field back out strips controls here and keeps the format characters.
    """
    return _SCAN_CONTROL_RE.sub("", value)


def normalize_for_scanning(value: str) -> str:
    """Return ``value`` with invisible characters removed, for a text scanner.

    A scanner that decides by matching a pattern needs this. An invisible character
    embedded mid-token splits that token, so a pattern describing the token cannot
    match it, and the scanner reaches its verdict on a string no consumer displays.

    Removing invisible characters JOINS their neighbours, and that cuts both ways. It
    reveals a token split by one of them. It can also destroy a boundary a pattern
    requires: a negative lookbehind for a non-word character is satisfied by the
    invisible character itself, so joining a word character onto the token defeats the
    match that the text as stored would have produced. This is therefore NOT a
    substitute for scanning the original -- a caller that rewrites what it matches
    scans both, the text as stored and the text normalised, and applies both verdicts.

    Controls go in a FIRST pass, before any format character is judged. The judgement
    below reads a run's neighbours, and a control character is itself non-ASCII: left in
    place it would masquerade as a load-bearing neighbour and keep a format character
    sitting inside an ASCII token, which is the whole defect.

    A run of invisible characters then goes only when the characters on BOTH sides of it
    are ASCII. That is the exact condition for an ASCII token to straddle the run: a
    token is contiguous, so if one sits across the run then the character each side of it
    belongs to that token and is ASCII. Either side being non-ASCII already breaks any
    ASCII token there, so removing the run buys nothing and only risks damage -- and a
    non-ASCII side is exactly where an invisible character does work: U+200D joins the
    parts of an emoji sequence, U+FE0F selects an emoji's presentation or encloses a
    keycap, U+200C shapes Persian and Indic text, and the BIDI marks order
    mixed-direction runs such as a Latin digit before Arabic.

    A MISSING side counts as non-ASCII, so a run at either end of the string is kept: no
    token straddles a run with nothing on one side of it, and a trailing variation
    selector is ordinary content.

    :func:`kiro_crew.preview_text.drop_format_chars` drops category ``Cf``
    unconditionally and is the one implementation of that; this keeps its membership
    test rather than re-enumerating the category, adds the invisible code points that
    are not ``Cf``, and diverges only on the condition. Its own reasoning is why: it
    accepts emoji sequences decomposing because it builds a ONE-LINE PREVIEW, where
    losing a joiner costs a glyph. This normalises a stored field that goes back out in
    full and that an edit can persist, so the same loss would rewrite the user's content.

    The result holds no invisible character anywhere an ASCII token could straddle it
    unseen. Only invisible characters are removed, so no visible content is lost; a token
    split by printable text stays split, and stays split downstream too.

    Tab, newline and carriage return are kept as content. A token split by one of
    those three is therefore still split after this call.
    """
    text = strip_control_characters(value)
    # The ASCII range holds no invisible code point, so ordinary traffic skips the walk.
    if text.isascii():
        return text
    kept: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if not _is_invisible(text[index]):
            kept.append(text[index])
            index += 1
            continue
        end = index
        while end < length and _is_invisible(text[end]):
            end += 1
        preceding = text[index - 1] if index else ""
        following = text[end] if end < length else ""
        if not (preceding and preceding.isascii() and following and following.isascii()):
            kept.append(text[index:end])
        index = end
    return "".join(kept)


def safe_terminal_line(value: str) -> str:
    """Return bounded text confined to ONE terminal line with no live controls.

    For renderers that print a prefix per line (``✅``/``⚠️``/``❌``), a newline
    in untrusted text would start an unprefixed line that reads as the CLI's own
    output. Newlines are rendered as the visible ``\\x0a`` literal (the convention
    ``doctor_deadpath`` already uses) before the length cap, so the cap applies to
    what is actually printed; tabs are kept, and carriage returns fall in the C0
    range the pattern strips.
    """
    cleaned = _TERMINAL_CTRL_RE.sub("", value).replace("\n", "\\x0a")
    if len(cleaned) > _TERMINAL_TEXT_MAX:
        return cleaned[: _TERMINAL_TEXT_MAX - 1] + "…"
    return cleaned

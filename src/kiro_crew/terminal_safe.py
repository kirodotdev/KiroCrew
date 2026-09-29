"""Terminal-safe rendering for untrusted text.

This module is a stdlib-only leaf so CLI, doctor, and lightweight HTTP clients
can share one control-sequence policy without importing each other.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "normalize_for_scanning",
    "scan_normalised_with_map",
    "safe_terminal_line",
    "strip_control_characters",
    "strip_controls_with_map",
]

# Strip complete OSC and CSI sequences, other two-byte ESC sequences, and C0/C1
# controls while preserving newlines and tabs. OSC must precede the generic ESC
# alternative so its payload is removed with its introducer and terminator.
_TERMINAL_CTRL_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC through BEL or ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI with the full ECMA-48 parameter class
    r"|\x1b[ -/]*[@-~]"  # other two-byte ESC sequences
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"  # C0/C1 controls (keep \n and \t)
)


# The scan normaliser's escape policy. It consumes a COMPLETE terminal escape
# sequence -- introducer, parameters and terminator together -- so a sequence
# spliced mid-token drops out entirely and the token rejoins for a pattern
# scanner. It is deliberately NARROWER than a "strip any ESC + final byte" rule:
# a LONE introducer (a bare ``\x1b`` or an 8-bit ``\x9b``) followed by ordinary
# text is a single stray control, NOT the opening of a sequence whose "final
# byte" is the next character -- treating it as a sequence would eat that
# character, and when the character belongs to a credential the eaten byte is
# exactly what the scan must preserve to still recognise the token. So a
# structured form is matched only through its real introducer syntax
# (``\x1b[`` / ``\x9b`` for CSI, ``\x1b]`` / ``\x9d`` for OSC, ``\x1bP`` /
# ``\x90`` for DCS, and the string-terminated C1 forms), and every other
# control -- a bare introducer included -- falls to the single-byte class.
#
# WIDER than ``_TERMINAL_CTRL_RE`` in exactly one direction: the 8-bit C1
# introducers open a sequence rather than being lone controls, so their trailing
# parameter bytes are consumed WITH them. A terminal renders ``a\x9b0mb`` as
# ``ab`` (the ``0m`` is the CSI parameter, not text), while ``_TERMINAL_CTRL_RE``
# drops only the ``\x9b`` byte and leaves ``0m`` behind as text -- the 8-bit blind
# spot this pattern closes. Tab, newline and carriage return are content and are
# kept, matching ``normalize_for_scanning`` (``_SCAN_CONTROL_RE``) rather than
# ``_TERMINAL_CTRL_RE``, which strips CR.
#
# Ordered longest-match-first: each structured sequence form precedes the
# single-control fallback so an introducer is consumed with its payload when it
# opens a real sequence, and stripped as one byte when it does not.
#: The escape-sequence alternatives, with two INDEPENDENT widenings a bare
#: introducer allows. ``two_byte_esc`` also consumes a complete two-byte ESC
#: (``\x1bM``); ``csi_no_params`` also consumes a bare 8-bit CSI with no parameter
#: byte (``\x9bm``). They are independent because one token can carry a split that
#: needs one widening AND another split that needs it OFF, so a caller scans all
#: four combinations and unions the redactions -- coupling the two into a single
#: "greedy" reading misses the mixed case.
def _scan_escape_source(*, two_byte_esc: bool, csi_no_params: bool) -> str:
    two_byte = r"|\x1b[ -/]*[@-~]" if two_byte_esc else ""  # complete two-byte ESC (\x1bM)
    csi_8bit = (
        r"|\x9b[0-?]*[ -/]*[@-~]"  # parameters optional
        if csi_no_params
        else r"|\x9b[0-?]*[ -/]+[@-~]|\x9b[0-?]+[@-~]"  # needs a parameter byte
    )
    return (
        r"\x1b\][^\x07\x1b\x9c\x18\x1a]*(?:\x07|\x9c|\x1b\\)"  # 7-bit OSC .. BEL/ST
        r"|\x1b[P^_][^\x1b\x9c\x18\x1a]*(?:\x9c|\x1b\\)"  # 7-bit DCS/PM/APC .. ST
        r"|\x1bX[^\x1b\x9c\x18\x1a]*(?:\x9c|\x1b\\)"  # 7-bit SOS .. ST
        r"|\x1b\[[0-?]*[ -/]*[@-~]"  # 7-bit CSI with the full ECMA-48 parameter class
        rf"{two_byte}"
        r"|\x9d[^\x07\x9c\x1b\x18\x1a\x9d]*(?:\x07|\x9c|\x1b\\)"  # 8-bit OSC .. BEL/ST
        r"|[\x90\x9e\x9f\x98][^\x9c\x1b\x18\x1a\x90\x9e\x9f\x98]*(?:\x9c|\x1b\\)"  # 8-bit DCS/PM/APC/SOS .. ST
        rf"{csi_8bit}"
        r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"  # lone C0/C1 + DEL (keep \t \n \r)
    )


# The four independent readings of a bare escape introducer: {two-byte ESC on/off}
# x {parameterless 8-bit CSI on/off}. A caller scans every one and unions the
# redactions, so a token carrying splits that need DIFFERENT readings is still
# reconstructed under one of them. Indexed by (two_byte_esc, csi_no_params).
_SCAN_ESCAPE_RES = {
    (two_byte_esc, csi_no_params): re.compile(
        _scan_escape_source(two_byte_esc=two_byte_esc, csi_no_params=csi_no_params)
    )
    for two_byte_esc in (False, True)
    for csi_no_params in (False, True)
}

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


def strip_controls_with_map(value: str, *, drop_invisibles: bool = False) -> tuple[str, list[int]]:
    """:func:`strip_control_characters`, plus each kept char's ORIGINAL index.

    This is the PER-CHARACTER strip -- it drops a C0/C1 control byte (and, with
    ``drop_invisibles``, a format/invisible character too) and keeps everything around
    it, so a printable escape-sequence payload (the ``0m`` of a CSI, an OSC's title text)
    SURVIVES. It is the reading :func:`scan_normalised_with_map` does not cover: that one
    consumes a whole sequence, payload included, so a credential split by a sequence whose
    payload it swallowed rejoins only under THIS strip. A span-mapping caller scans both
    and unions them.

    ``drop_invisibles`` composes the control strip WITH the invisible-run removal in one
    per-character pass, so a token split by BOTH a control byte (whose sequence a
    whole-sequence reading would consume, payload and all) AND a zero-width separator
    rejoins here -- exactly the reassembly a renderer performs, and a split neither the
    control-only strip nor the invisible-run removal alone reconstructs.

    The map is monotonic with a trailing sentinel ``result_map[len(result)] == len(value)``,
    exactly like :func:`scan_normalised_with_map`, so a half-open span ``[i, j)`` on the
    stripped string maps to original bytes ``[result_map[i], result_map[j - 1] + 1)``.
    """

    def _dropped(character: str) -> bool:
        if _SCAN_CONTROL_RE.match(character) is not None:
            return True
        return drop_invisibles and _is_invisible(character)

    if not any(_dropped(character) for character in value):
        return value, list(range(len(value) + 1))
    kept_chars: list[str] = []
    kept_map: list[int] = []
    for index, character in enumerate(value):
        if not _dropped(character):
            kept_chars.append(character)
            kept_map.append(index)
    kept_map.append(len(value))
    return "".join(kept_chars), kept_map


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


def scan_normalised_with_map(
    value: str, *, two_byte_esc: bool = False, csi_no_params: bool = False
) -> tuple[str, list[int]]:
    """Return ``value`` normalised for scanning, plus each kept char's ORIGINAL index.

    This is :func:`normalize_for_scanning` with two differences a span-mapping
    redactor needs, and nothing else changes what is kept versus dropped.

    ``two_byte_esc`` and ``csi_no_params`` are two INDEPENDENT widenings of what a
    bare escape introducer means, which a caller scans in all FOUR combinations and
    unions. Both default ``False`` -- a lone ``\\x1b`` or 8-bit ``\\x9b`` with no
    structured body is a single stray control and the character after it survives,
    so a credential byte immediately after a bare introducer is never eaten. With
    ``two_byte_esc`` a complete two-byte ESC (``\\x1bM``) is consumed WHOLE; with
    ``csi_no_params`` a bare 8-bit CSI (``\\x9bm``, no parameter byte) is consumed
    WHOLE. They are independent because ONE token can carry a split that needs one
    widening AND a split that needs it off, so no single reading is safe: the union
    of all four is what reconstructs every such token while never eating a credential
    byte a different split needed kept.

    First, it consumes a terminal escape sequence as ONE unit -- introducer,
    parameters and terminator together -- so the parameter bytes of an 8-bit C1
    sequence (``\\x9b0m``, ``\\x9d…\\x9c``) do not survive as text the way
    :func:`normalize_for_scanning`'s per-character control strip leaves them. A
    scanner that decides by pattern match needs the whole sequence gone, or the
    token a mid-token sequence split does not rejoin. Where ``normalize_for_scanning``
    strips a control PER CHARACTER -- leaving the printable parameter bytes of an
    escape sequence behind as text -- this removes the whole sequence, introducer
    and payload together, which is the difference that closes the blind spot. Tab,
    newline and carriage return are content and survive, exactly as there.

    Second, it returns an index map. Because normalisation only ever DELETES
    characters -- never inserts or reorders -- position ``i`` of the returned
    string came from position ``result_map[i]`` of ``value``, and the map is
    strictly increasing. A trailing sentinel ``result_map[len(result)] ==
    len(value)`` lets a half-open span ``[i, j)`` on the normalised string map to
    original bytes ``[result_map[i], result_map[j - 1] + 1)`` -- from the first
    matched character's origin to just past the last matched character's, the end
    mapping :func:`kiro_crew.security._map_spans_back` applies so a control byte
    deleted AFTER the match is not swept into the span.

    A caller redacts the ORIGINAL bytes in place: it scans this normalised copy,
    computes the redaction spans on it, maps those spans back through this index,
    and rewrites only the mapped ranges of ``value``. That is what keeps a
    byte-fidelity caller's stored bytes unchanged except where a credential lives.
    """
    # Fast path: an all-ASCII value with no control byte (bar tab/newline/CR) has
    # nothing to strip, so it maps to itself. This keeps the common egress case --
    # ordinary prose scanned on the event loop -- a single C-level regex search
    # instead of the per-character Python walk below.
    if value.isascii() and _SCAN_CONTROL_RE.search(value) is None:
        return value, list(range(len(value) + 1))
    # Drop whole escape sequences first, threading each surviving char's original
    # index. A dropped sequence contributes no map entries; a kept char contributes
    # its own original position.
    stripped_chars: list[str] = []
    stripped_map: list[int] = []
    cursor = 0
    length = len(value)
    escape_re = _SCAN_ESCAPE_RES[(two_byte_esc, csi_no_params)]
    while cursor < length:
        m = escape_re.match(value, cursor)
        if m is not None and m.end() > cursor:
            cursor = m.end()
            continue
        stripped_chars.append(value[cursor])
        stripped_map.append(cursor)
        cursor += 1

    # Then remove invisible/format runs an ASCII token could straddle, mirroring
    # normalize_for_scanning's condition exactly, but over the stripped char list
    # so the surviving map stays aligned.
    kept_chars: list[str] = []
    kept_map: list[int] = []
    n = len(stripped_chars)
    i = 0
    while i < n:
        ch = stripped_chars[i]
        if not _is_invisible(ch):
            kept_chars.append(ch)
            kept_map.append(stripped_map[i])
            i += 1
            continue
        end = i
        while end < n and _is_invisible(stripped_chars[end]):
            end += 1
        preceding = stripped_chars[i - 1] if i else ""
        following = stripped_chars[end] if end < n else ""
        if not (preceding and preceding.isascii() and following and following.isascii()):
            kept_chars.extend(stripped_chars[i:end])
            kept_map.extend(stripped_map[i:end])
        i = end

    kept_map.append(length)  # sentinel so [i, j) maps without a bounds check
    return "".join(kept_chars), kept_map


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

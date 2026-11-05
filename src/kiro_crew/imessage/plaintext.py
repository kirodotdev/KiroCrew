"""Markdown-to-plaintext conversion and message splitting for iMessage.

iMessage renders no markup at all, so an answer written in markdown arrives as
literal asterisks, backticks and pipe tables. This module flattens it, with one
exception that matters more than the rest: **the contents of a fenced code block
pass through verbatim**. Code is the payload a user is most likely to copy out
of a message, and any "cleanup" applied to it -- unwrapping, re-indenting,
collapsing blank lines -- corrupts it silently.

Splitting is separate from conversion and runs last, on the already-flat text,
so a chunk boundary can never land inside markup that conversion was about to
remove. It prefers a paragraph break, then a line break, then a space, and
falls back to a hard cut -- which CJK text, having no spaces, always reaches.
A hard cut still respects grapheme clusters: cutting a flag, a skin-toned
emoji, or a combining accent in half produces visible mojibake on both sides.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

from kiro_crew.constants import md_link_destination

#: Fence delimiter line: three or more backticks or tildes, optional info string.
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*(\S*)\s*$")

#: ATX heading marker, e.g. "### ".
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")

#: Setext-style horizontal rule: --- / *** / ___ on its own line.
_RULE_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")

#: Unordered list marker.
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")

#: Ordered list marker, kept as-is but normalized to "N. ".
_ORDERED_RE = re.compile(r"^(\s*)(\d{1,9})[.)]\s+")

#: Blockquote marker, possibly nested.
_QUOTE_RE = re.compile(r"^\s{0,3}(?:>\s?)+")

#: Image before link, so the "!" is consumed rather than left dangling.
#: The label class excludes "[", "]" and the line break, the display-safety
#: screen's class, so the match cannot span two links; the target class
#: excludes whitespace -- both keep the pattern linear-time on adversarial
#: input. A parenthesis may appear in the target only as a balanced pair
#: (``.../Python_(programming_language)``); see
#: :func:`kiro_crew.constants.md_link_destination`.
_LINK_DESTINATION_CHAR_CLASS = r"[^\s()]"
_IMAGE_RE = re.compile(
    rf"!\[([^\[\]\n]*)\]\(({md_link_destination(_LINK_DESTINATION_CHAR_CLASS)}*)\)"
)
_LINK_RE = re.compile(
    rf"\[([^\[\]\n]*)\]\(({md_link_destination(_LINK_DESTINATION_CHAR_CLASS)}*)\)"
)

#: Inline code span. Non-greedy over a single backtick pair.
_CODE_SPAN_RE = re.compile(r"`([^`]*)`")

#: A bare URL, kept verbatim through the emphasis passes. It runs to the next
#: whitespace; :func:`_keep_bare_url` decides which emphasis marker in it closes
#: an opener before the URL.
_BARE_URL_RE = re.compile(r"https?://[^\s<>\x00]+")

#: A maximal run of one emphasis character.
_MARKER_RUN_RE = re.compile(r"\*+|_+|~+")

#: A token :func:`_flatten_inline` swaps a verbatim span for.
_KEPT_TOKEN_RE = re.compile(r"\x00(\d+)\x00")

#: Emphasis, longest marker first so "**" is not eaten as two "*".
_EMPHASIS_RES = (
    re.compile(r"\*\*\*([^*]+)\*\*\*"),
    re.compile(r"\*\*([^*]+)\*\*"),
    re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])"),
    re.compile(r"___([^_]+)___"),
    re.compile(r"__([^_]+)__"),
    re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])"),
    re.compile(r"~~([^~]+)~~"),
)

#: Three or more consecutive newlines collapse to a paragraph break.
_BLANK_RUN_RE = re.compile(r"\n{3,}")

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
#: U+0E47), Khmer and the Indic matras -- so testing the class alone declared
#: those marks safe to cut after and split a grapheme the reader sees as one
#: character. Testing the CATEGORY is the property that actually holds: a mark
#: belongs to the preceding base whatever its class. Erring wide here can only
#: move a cut earlier, never split a cluster, so the whole file's guarantee
#: ("never separate a mark from its base") gets stronger, not weaker.
_MARK_CATEGORIES = frozenset({"Mn", "Mc", "Me"})


def to_plaintext(text: str) -> str:
    """Flatten markdown to plain text, passing code-block contents through.

    Block structure is decided line by line so a fenced region can be handed
    over untouched; inline markup is only stripped outside those regions.
    """
    if not text:
        return ""
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        match = _FENCE_RE.match(line)
        if match is not None:
            marker = match.group(1)
            if fence is None:
                # Keep the opener's ACTUAL run, not a normalised three. Per
                # CommonMark a fence is closed only by a run of the same
                # character that is at least as long, so a ``` line inside a
                # ```` block is content. Normalising to three made that inner
                # line close the block, and everything after it was then
                # markdown-flattened -- silent corruption of the code the user
                # was shown.
                fence = marker
                continue
            if marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
                continue
            # Same character but shorter, or a different delimiter entirely:
            # content inside the fence, not a closer.
            out.append(line)
            continue
        if fence is not None:
            out.append(line)
            continue
        out.append(_flatten_block(line))
    # Trim blank lines only. A bare .strip() would eat the indentation of a
    # leading code line, which is semantic inside a fence.
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(out)).strip("\n")


def _flatten_block(line: str) -> str:
    """Flatten one non-code line: block markers first, then inline markup."""
    if _RULE_RE.match(line):
        return ""
    line = _QUOTE_RE.sub("", line)
    line = _HEADING_RE.sub("", line)
    bullet = _BULLET_RE.match(line)
    if bullet is not None:
        line = f"{bullet.group(1)}• {line[bullet.end():]}"
    else:
        ordered = _ORDERED_RE.match(line)
        if ordered is not None:
            line = f"{ordered.group(1)}{ordered.group(2)}. {line[ordered.end():]}"
    return _flatten_inline(line)


def _flatten_inline(line: str) -> str:
    # Inline code, link targets and bare URLs are shown as written, so they sit
    # out the emphasis passes: ``__init__`` in backticks or in
    # ``https://e.com/__init__.py`` keeps its underscores. Each is swapped for a
    # NUL-delimited token and put back last; a line that already holds a NUL is
    # flattened without the swap, so input text can never pose as a token.
    kept: list[str] = []

    def keep(value: str) -> str:
        kept.append(value)
        return f"\x00{len(kept) - 1}\x00"

    protect = "\x00" not in line
    if protect:
        line = _CODE_SPAN_RE.sub(lambda m: keep(m.group(1)), line)
    else:
        line = _CODE_SPAN_RE.sub(r"\1", line)

    # A link's URL is kept: it is often the actionable part of the answer, and
    # iMessage makes a bare URL tappable.
    def link(m: re.Match[str]) -> str:
        label, target = m.group(1).strip(), m.group(2).strip()
        # A target already holding a code-span token is restored by the final
        # pass as it stands; keeping it whole would hide that inner token.
        shown = keep(target) if protect and target and "\x00" not in target else target
        if not label or label == target:
            return shown or label
        if not target:
            return label
        return f"{label} ({shown})"

    line = _IMAGE_RE.sub(link, line)
    line = _LINK_RE.sub(link, line)
    if protect:
        line = _BARE_URL_RE.sub(lambda m: _keep_bare_url(m, keep), line)
    for pattern in _EMPHASIS_RES:
        line = pattern.sub(r"\1", line)
    if protect:
        line = _KEPT_TOKEN_RE.sub(lambda m: kept[int(m.group(1))], line)
    return line.rstrip()


def _keep_bare_url(m: re.Match[str], keep: Callable[[str], str]) -> str:
    """Keep a bare URL, leaving outside it a marker run that closes an opener.

    Underscores and asterisks belong to the URL (``.../__tests__``) unless the
    text before the URL holds an unclosed run of the same marker: then the last
    such run inside the match is that opener's closer (``**see https://e.com**.``),
    and it and everything after it stay outside the kept token for the emphasis
    passes.
    """
    url = m.group(0)
    open_runs = _unclosed_marker_runs(m.string[: m.start()])
    cut = len(url)
    for run in _MARKER_RUN_RE.finditer(url):
        if run.group(0) in open_runs:
            cut = run.start()
    return keep(url[:cut]) + url[cut:]


def _unclosed_marker_runs(prefix: str) -> set[str]:
    """Marker runs that occur an odd number of times in ``prefix``."""
    counts: dict[str, int] = {}
    for run in _MARKER_RUN_RE.finditer(prefix):
        counts[run.group(0)] = counts.get(run.group(0), 0) + 1
    return {run for run, n in counts.items() if n % 2}


def chunk_plaintext(text: str, limit: int) -> list[str]:
    """Split flat text into chunks of at most ``limit`` characters.

    Returns ``[]`` for empty input, and a single chunk when ``limit`` is
    non-positive -- a caller with a broken limit should still get its message
    delivered rather than silently dropped.
    """
    if not text:
        return []
    if limit <= 0:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = _preferred_cut(remaining, limit)
        # Asymmetric on purpose. Trailing whitespace is never semantic here (the
        # text is already flattened and iMessage renders plain text), so the head
        # is fully rstripped. The LEADING side is the dangerous one: this function
        # cannot tell code from prose, and a bare lstrip destroyed the indentation
        # of any code line that fell on a chunk boundary. So only newlines come
        # off the front.
        head = remaining[:cut].rstrip()
        if head:
            chunks.append(head)
        remaining = remaining[cut:].lstrip("\n")
        if not remaining:
            return chunks
    tail = remaining.rstrip()
    if tail:
        chunks.append(tail)
    return chunks


#: Each break tier and the divisor of ``limit`` its cut must reach. These
#: follow ``messaging.split.split_markdown_safe``'s ladder (a paragraph break
#: at least halfway in, a line break at least a quarter in), and a space takes
#: the line tier's floor. A break nearer the start would strand most of the
#: budget: ``"Hi\n\n"`` plus one long answer becomes three messages, not two.
_BREAK_TIERS: tuple[tuple[str, int], ...] = (("\n\n", 2), ("\n", 4), (" ", 4))


def _preferred_cut(text: str, limit: int) -> int:
    """Best split index within ``limit``: paragraph, then line, then space.

    A break counts only when its cut reaches its tier's share of ``limit``;
    an earlier one falls through to the next-weaker tier. With no tier left,
    the last break in reach is still taken when the word after it fits one
    chunk, so a long URL behind a short lead-in moves whole; a word too long
    for any chunk is cut anyway, so the budget is filled instead. A hard cut is
    pulled back to a grapheme boundary; one that collapses to zero emits the
    first grapheme whole instead of producing an empty chunk and looping.
    """
    window = text[: limit + 1]
    for separator, divisor in _BREAK_TIERS:
        index = window.rfind(separator)
        if index > 0 and index + len(separator) >= limit // divisor:
            return index + len(separator)
    last_break = max(window.rfind("\n"), window.rfind(" "))
    if last_break > 0:
        start = last_break + 1
        if _word_end(text, start, start + limit + 1) - start <= limit:
            return start
    cut = _grapheme_boundary(text, limit)
    # A single grapheme longer than the whole limit (a long emoji ZWJ
    # sequence with a tiny limit) has no safe boundary below it; emit it whole
    # rather than splitting it or spinning forever on a zero-width cut.
    return cut if cut > 0 else _grapheme_end(text, limit)


def _word_end(text: str, start: int, reach: int) -> int:
    """First space or newline in ``text[start:reach]``, else ``reach`` capped at the end."""
    ends = [i for i in (text.find(" ", start, reach), text.find("\n", start, reach)) if i >= 0]
    return min(ends) if ends else min(reach, len(text))


def _grapheme_boundary(text: str, index: int) -> int:
    """Largest boundary <= ``index`` that does not split a grapheme cluster."""
    if index >= len(text):
        return len(text)
    while index > 0 and _joins_previous(text, index):
        index -= 1
    return index


def _grapheme_end(text: str, index: int) -> int:
    """Smallest boundary > ``index`` that does not split a grapheme cluster."""
    end = max(index, 1)
    while end < len(text) and _joins_previous(text, end):
        end += 1
    return end


def _joins_previous(text: str, index: int) -> bool:
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

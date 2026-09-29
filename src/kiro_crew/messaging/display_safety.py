"""Redaction against what a chat platform will DISPLAY, not the bytes it is sent.

Every channel scans outbound text for credentials, but a scan of the literal
bytes is not enough on any platform that renders markup away: ``AKIA**REST**``
and ``[AKIA](https://x)REST`` match no credential pattern as written, yet the
reader sees an intact key once the delimiters are stripped at render time. The
transformation happens AFTER the scan, so the scan has to anticipate it.

This lives in ``messaging`` rather than in one channel package because the
hazard is not Slack-specific: Telegram (MarkdownV2) and Discord (Markdown)
collapse the same emphasis, code-span and link syntax. It was written for
Slack first and hoisted here when :func:`kiro_crew.messaging.renderer.
format_overflow` began putting LLM-authored choice text into the message BODY
on every widget-capable channel -- the shared sink cannot depend on each
renderer remembering to canonicalise, which is the same reasoning that put the
``max_buttons`` cap in shared code.

Stdlib-only leaf: it takes the redactor as a parameter rather than importing
``kiro_crew.security``, so it stays importable from anywhere and each caller
keeps its own (possibly session-scoped) redactor.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import Counter
from typing import Callable, Iterator, Sequence

from kiro_crew.constants import md_link_destination
from kiro_crew.preview_text import drop_format_chars

_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")

# Delimiter runs the platforms consume at render time. ``||`` is Discord's
# spoiler: the reader clicks it and the delimiters vanish, joining the halves --
# the same splitter property as ``**``, which is why it belongs in this run
# rather than in a pass of its own.
#
# The pipe counts only in PAIRS. A lone ``|`` is literal text on every channel
# here (Telegram's body goes out as HTML, where ``||`` is not spoiler markup
# either, and Slack renders it as-is), so collapsing single pipes would only
# widen the canonical form for no display that matches it. Slack link internals
# (``<url|label>``) are already consumed by ``_SLACK_LINK`` before this runs.
_EMPHASIS_RUN = re.compile(r"(?:[*_~`]|\|\|)+")

# Telegram's plain-text fallback (``telegram.renderer._strip_md``) is the only
# sink that removes inline markup after the Markdown screen. Every pass of it
# that can join two runs of text is defined here and imported by the renderer,
# which imports this module, so the screen and the sink read one definition and
# cannot drift: the fence, inline-code, ``**`` and ``__`` passes drop the pair
# around their content, and the link pass drops the ``[`` and ``]`` around its
# label, which is how ``AKIA[IOSF...]**(https://x)**`` joins once the ``**`` is
# gone and the link closes. The fallback's heading pass can remove the marker
# between a credential field name and its assignment punctuation, so it shares
# this sequence too. The bullet pass replaces its marker with a visible bullet
# and remains the renderer's own. The fence pattern's separate newline and
# same-line alternatives keep an unclosed opener linear; its content group is
# ``None`` for a same-line fence, which a ``\1`` template reads as empty and a
# callable must read as ``match.group(1) or ""``.
TELEGRAM_FALLBACK_FENCE = re.compile(r"```(?:[^\n]*\n(.*?)|[^\n]*)```", re.DOTALL)
TELEGRAM_FALLBACK_INLINE_CODE = re.compile(r"`([^`\n]+)`")
TELEGRAM_FALLBACK_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.MULTILINE)


def telegram_fallback_heading_text(match: re.Match[str]) -> str:
    """Return the heading body exactly as Telegram's plaintext fallback shows it."""
    return match.group(1).strip()


TELEGRAM_FALLBACK_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
TELEGRAM_FALLBACK_BOLD_USCORE = re.compile(r"__(.+?)__", re.DOTALL)
# The link the fallback flattens and the HTML translation links: the screen's
# label class (no ``[``, ``]`` or line break, see ``_MD_LINK`` below) and the
# shared destination unit, with the scheme the renderer requires and no
# whitespace in the url. A balanced pair may sit inside the url; see
# :func:`kiro_crew.constants.md_link_destination`.
_TELEGRAM_LINK_DESTINATION_CHAR_CLASS = r"[^()\s]"
TELEGRAM_FALLBACK_LINK = re.compile(
    rf"\[([^\[\]\n]+)\]\((https?://{md_link_destination(_TELEGRAM_LINK_DESTINATION_CHAR_CLASS)}+)\)"
)
#: What the fallback prints for a link: the label, then the url in parentheses.
TELEGRAM_FALLBACK_LINK_TEXT = r"\1 (\2)"
#: The shared passes in the relative order the fallback applies them, each with
#: the replacement the fallback prints.
TELEGRAM_FALLBACK_PASSES: tuple[
    tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...
] = (
    (TELEGRAM_FALLBACK_FENCE, r"\1"),
    (TELEGRAM_FALLBACK_INLINE_CODE, r"\1"),
    (TELEGRAM_FALLBACK_HEADING, telegram_fallback_heading_text),
    (TELEGRAM_FALLBACK_BOLD_STAR, r"\1"),
    (TELEGRAM_FALLBACK_BOLD_USCORE, r"\1"),
    (TELEGRAM_FALLBACK_LINK, TELEGRAM_FALLBACK_LINK_TEXT),
)
# ``[label](url)`` (Markdown) and ``<url|label>`` (Slack mrkdwn). Both DISPLAY only
# the label, so on a rendering platform the url is invisible to a reader and the
# label joins whatever surrounds it -- which makes them a splitter, exactly like
# ``**``. A plain-text sink prints the url beside the label instead, so
# :func:`redact_for_display` scans that reading as well.
#
# The opening delimiter is excluded from every inner class (no ``[`` inside the
# Markdown label, no ``<`` inside the Slack one). That is not cosmetic: with ``[``
# allowed, input like ``[[[[[[...`` makes each start position consume the whole
# remaining string before failing to find ``]``, so the scan is quadratic in the
# length of attacker-supplied text (CodeQL ``py/polynomial-redos``). Excluding it
# makes a failed start fail immediately, which matters because this runs on every
# outbound message. A label containing a literal ``[`` is simply not collapsed --
# safe, since the fallback is to scan the text as written.
#
# The Markdown url is built from the SAME unit every channel renderer builds its
# link destination from (:func:`kiro_crew.constants.md_link_destination`), so a
# balanced pair of parentheses may sit inside it. A url a renderer links but this
# leaves raw is a split the scan never sees whole: ``[AKIA](https://x/a_(b))REST``
# shows ``AKIAREST`` on Slack. The unit's classes exclude both parentheses, so no
# character can be consumed two ways and the scan stays linear.
_MD_LINK_DESTINATION_CHAR_CLASS = r"[^()\n]"
_MD_LINK = re.compile(
    rf"\[([^\[\]\n]*)\]\(({md_link_destination(_MD_LINK_DESTINATION_CHAR_CLASS)}*)\)"
)
_SLACK_LINK = re.compile(r"<([^<>|\n]*)\|([^<>\n]*)>")

#: How many consecutive messages one INTERIOR reading may span in
#: :func:`severs_a_credential`. A credential framed by a spoiling message on each
#: side appears in no prefix, suffix or whole reading, so those runs are read too --
#: but capped, because reading every contiguous run is quadratic in a piece count
#: the author controls. Four covers a credential the split broke into three
#: fragments, which is what markup spanning a length cut produces; the cap is what
#: keeps the reading count linear in the number of messages.
_INTERIOR_RUN_PIECES = 4
# How many times an emitted canonical form is rendered again before it is taken
# to render as itself. Honest input settles in two passes; a link nested one
# level inside another takes three. The bound is what keeps the cost of an
# outbound message fixed against text built to keep unmasking a link per pass.
DISPLAY_SETTLING_PASSES = 4
# Every character a display reading treats as markup: the link, Slack link,
# emphasis, code and spoiler delimiters. Text without them outside a redaction
# tag leaves a renderer nothing to act on but a tag's own ``]`` beside a ``(``,
# which the fallback below scans for once more. Parentheses are not markup: a
# last-resort reply keeps its words, and stripping them would take them from
# every word.
DISPLAY_MARKUP = re.compile(r"[\[\]<>|*_~`]")
# Tags written by the redactors: two fixed credential tags and the suspicious-URL
# tag carrying its source domain. The domain admits DNS, IPv4 and unbracketed IPv6
# characters or an exact bracketed IPv6 literal, but no display markup, heading
# marker, whitespace or format character. A span of this shape is kept whoever wrote
# it, so nothing a reading could act on may enter it. An underscore is emphasis
# markup and stays out even though a DNS name can hold one: such a suspicious-URL
# tag is not kept whole on the last-resort paths, which costs its brackets and its
# count in the redaction notice, never the redaction. This shape is local because
# this leaf cannot import the security package; ``test_markdown_link_parentheses.py``
# pins it against that package's exported vocabulary.
_REDACTION_TAG = re.compile(
    r"\[REDACTED: (?:(?:encoded )?credential|suspicious URL to "
    r"(?:[-A-Za-z0-9.:]+|\[[0-9A-Fa-f:.]+\]))\]"
)


def strip_ansi(text: str) -> str:
    """Remove SGR colour escapes.

    Public because redaction call sites need it: this strip can *reassemble* a
    credential that escape sequences had broken up, so a caller that redacts
    around a conversion has to normalise with the SAME function first, or the
    secret slips through the regex and is put back together afterwards.
    """
    return _ANSI_SGR.sub("", text)


def _strip_format_chars(text: str) -> str:
    """Drop Unicode *format* characters (category ``Cf``) and soft hyphens.

    The delimiter families above are visible markup a platform consumes. This is
    the invisible half of the same hazard, and it is strictly worse: a
    zero-width space, joiner, bidi mark or BOM between two halves of a key is
    rendered as NOTHING, so the reader sees an intact credential with no click
    and no markup, while every literal scan sees it broken. ``Cf`` is the
    principled set -- it is exactly Unicode's "format" category (ZWSP, ZWNJ,
    ZWJ, word joiner, bidi controls, BOM, soft hyphen) and contains nothing a
    reader can see.

    Delegates to :func:`kiro_crew.preview_text.drop_format_chars`, the one
    implementation of the Cf drop (its docstring carries the fast-path
    soundness argument), so the display-safety canonicalizer and the preview
    stripper cannot drift apart on which characters count as invisible.
    """
    return drop_format_chars(text)


def canonicalize_display(text: str) -> str:
    """Reduce *text* to what the platform will actually SHOW a reader.

    Three families, one property: the platform removes them at render time, so a
    credential broken across them is whole on screen while every literal scan
    sees it broken.

    * **links** use the one-level destination grammar our Slack, Telegram,
      WhatsApp and iMessage renderers use and collapse to their label;
    * **emphasis / code / spoiler delimiters** vanish -- ``AKIA**REST**`` and
      Discord's ``AKIA||REST||`` likewise;
    * **invisible format characters** were never rendered at all -- see
      :func:`_strip_format_chars`.

    Broader link readings are scanned separately by :func:`redact_for_display`.
    Links are reduced FIRST: a url can itself contain ``_`` or ``~``, and dropping
    those before the url is removed would corrupt the label boundaries. Format
    characters are dropped LAST, so a delimiter run that a zero-width character
    had split (``*``+ZWSP+``*``) is still recognised as the run it renders as.
    """
    return _display_form(text, lambda value: _MD_LINK.sub(r"\1", value))


def _display_form(text: str, collapse: Callable[[str], str]) -> str:
    """Apply *collapse* and the joining passes shared by display readings."""
    out = collapse(text)
    out = _SLACK_LINK.sub(r"\2", out)
    out = _EMPHASIS_RUN.sub("", out)
    return _strip_format_chars(out)


# The characters each per-line link walk acts on: an escape pair (the backslash
# and the character it hides, consumed as one unit) and the brackets the walk
# pairs. Ordinary text matches neither, so a walk jumps from one match to the
# next instead of stepping through every character in Python; the walks are
# graded over every window of a split, so that constant is paid per window.
_BALANCED_WALK_STEP = re.compile(r"\\.|[\[\]()]", re.S)
_FIRST_CLOSE_WALK_STEP = re.compile(r"\\.|[\[\]]", re.S)
# An escape pair or an angle bracket. Only the bracket matches are kept, which
# marks escapes left to right exactly as the walks consume them.
_ANGLE_DELIMITER = re.compile(r"\\.|[<>]", re.S)


def _unescaped_angle_positions(line: str) -> tuple[list[int], list[int]]:
    """Positions of every unescaped ``<`` and ``>`` in *line*, ascending."""
    less: list[int] = []
    greater: list[int] = []
    for match in _ANGLE_DELIMITER.finditer(line):
        if match.group() == "<":
            less.append(match.start())
        elif match.group() == ">":
            greater.append(match.start())
    return less, greater


def _first_at_or_after(positions: list[int], start: int, default: int) -> int:
    """The first of the ascending *positions* at or after *start*, else *default*."""
    at = bisect_left(positions, start)
    return positions[at] if at < len(positions) else default


def _collapse_balanced_links_on_line(line: str) -> str:
    """Collapse outermost links that close under the balanced-destination walk.

    Linear in the line: the walk visits only escape pairs and brackets, an
    angle destination jumps to its unescaped ``>`` through positions collected
    once per line and only for a line that has an angle destination, and every
    jump moves forward.
    """
    angle_positions: tuple[list[int], list[int]] | None = None
    stack: list[tuple[str, int, int]] = []
    closed: list[tuple[int, int, str]] = []
    label_start: int | None = None
    index = 0

    # Ordinary text is neither an escape nor a bracket: skip straight to the
    # next character the walk acts on.
    while (step := _BALANCED_WALK_STEP.search(line, index)) is not None:
        index = step.start()
        character = line[index]
        if character == "\\":
            index = step.end()
            continue
        if character == "[":
            label_start = index
            index += 1
            continue
        if character == "]":
            if label_start is not None and index + 1 < len(line) and line[index + 1] == "(":
                stack.append(("opener", label_start, index))
                destination_start = index + 2
                label_start = None
                index = destination_start
                if index < len(line) and line[index] == "<":
                    if angle_positions is None:
                        angle_positions = _unescaped_angle_positions(line)
                    less_positions, greater_positions = angle_positions
                    greater = _first_at_or_after(greater_positions, index + 1, len(line))
                    if greater < _first_at_or_after(less_positions, index + 1, len(line)):
                        index = greater + 1
                continue
            label_start = None
            index += 1
            continue
        if character == "(":
            stack.append(("paren", -1, -1))
        elif character == ")" and stack:
            kind, start, label_end = stack.pop()
            if kind == "opener":
                closed.append((start, index + 1, line[start + 1 : label_end]))
        index += 1

    if not closed:
        return line
    pieces: list[str] = []
    cursor = 0
    for start, end, label in sorted(closed, key=lambda span: (span[0], -span[1])):
        if start < cursor:
            continue
        pieces.append(line[cursor:start])
        pieces.append(label)
        cursor = end
    pieces.append(line[cursor:])
    return "".join(pieces)


def _collapse_links_at_balanced_close(text: str) -> str:
    """Collapse balanced Markdown links in one linear pass per line."""
    if "](" not in text:
        return text
    pieces: list[str] = []
    start = 0
    while start < len(text):
        newline = text.find("\n", start)
        if newline < 0:
            pieces.append(_collapse_balanced_links_on_line(text[start:]))
            break
        pieces.append(_collapse_balanced_links_on_line(text[start:newline]))
        pieces.append("\n")
        start = newline + 1
    return "".join(pieces)


def _balanced_link_reading(text: str) -> str:
    """Read links at their depth-unbounded balanced or angle destination close."""
    return _display_form(text, _collapse_links_at_balanced_close)


def _collapse_first_close_links_on_line(line: str) -> str:
    """Collapse non-overlapping links at the first later ``)`` in linear time.

    The walk visits only escape pairs and square brackets, each ``](`` finds
    its ``)`` with one forward search that the walk then jumps past, and a
    search that finds none ends the walk: no later opener can close either.
    """
    pieces: list[str] = []
    cursor = 0
    label_start: int | None = None
    index = 0
    # Ordinary text is neither an escape nor a square bracket: skip straight to
    # the next character the walk acts on.
    while (step := _FIRST_CLOSE_WALK_STEP.search(line, index)) is not None:
        index = step.start()
        character = line[index]
        if character == "\\":
            index = step.end()
            continue
        if character == "[":
            label_start = index
        elif character == "]":
            if label_start is not None and index + 1 < len(line) and line[index + 1] == "(":
                link_close = line.find(")", index + 2)
                if link_close < 0:
                    break
                pieces.append(line[cursor:label_start])
                pieces.append(line[label_start + 1 : index])
                cursor = link_close + 1
                index = cursor
                label_start = None
                continue
            label_start = None
        index += 1
    pieces.append(line[cursor:])
    return "".join(pieces)


def _collapse_links_at_first_close(text: str) -> str:
    """Collapse first-close Markdown links independently on each line."""
    if "](" not in text:
        return text
    pieces: list[str] = []
    start = 0
    while start < len(text):
        newline = text.find("\n", start)
        if newline < 0:
            pieces.append(_collapse_first_close_links_on_line(text[start:]))
            break
        pieces.append(_collapse_first_close_links_on_line(text[start:newline]))
        pieces.append("\n")
        start = newline + 1
    return "".join(pieces)


def _first_close_reading(text: str) -> str:
    """Read each Markdown link at the first ``)`` after its opener."""
    return _display_form(text, _collapse_links_at_first_close)


def _link_free_reading(text: str) -> str:
    """Read display markup while leaving every Markdown destination visible."""
    return _display_form(text, lambda value: value)


def _plain_reading(text: str) -> str:
    """Model Telegram's plain fallback after the Markdown screen has run.

    Apply every joining pass of the fallback (:data:`TELEGRAM_FALLBACK_PASSES`,
    the pattern objects and replacements ``_strip_md`` applies) in its relative
    order: fence, inline code, heading, ``**``, ``__``, then the link. The link
    pass has to follow the delimiter passes as it does in the fallback, because a
    delimiter pair can hold a link's ``(url)`` apart from its ``]``:
    ``AKIA[IOSF...]**(u)**`` is text to the screen's link pass, which runs first,
    and a link to the fallback, which flattens it to ``AKIAIOSF... (u)`` after
    dropping the ``**``. The heading pass removes a marker that can separate a
    credential field name from its assignment punctuation. The bullet pass leaves
    a visible bullet in place and remains outside the shared sequence. Invisible
    format characters are still dropped last.
    """
    out = text
    for construct, shown in TELEGRAM_FALLBACK_PASSES:
        out = construct.sub(shown, out)
    return _strip_format_chars(out)


SLACK_MARKDOWN_LINK = re.compile(rf"(?<!!)\[([^\[\]\n]+)\]\(({md_link_destination('[^()|>]')}+)\)")
SLACK_MRKDWN_BOLD = re.compile(r"\*([^*\n]+)\*")
SLACK_MRKDWN_ITALIC = re.compile(r"_([^_\n]+)_")
SLACK_MRKDWN_STRIKE = re.compile(r"~([^~\n]+)~")
SLACK_MRKDWN_EMPHASIS: tuple[re.Pattern[str], ...] = (
    SLACK_MRKDWN_BOLD,
    SLACK_MRKDWN_ITALIC,
    SLACK_MRKDWN_STRIKE,
)


def _slack_non_code_reading(text: str) -> str:
    """Collapse the Slack constructs rendered outside an inline-code span."""
    out = _SLACK_LINK.sub(r"\2", text)
    for emphasis in SLACK_MRKDWN_EMPHASIS:
        out = emphasis.sub(r"\1", out)
    return out


def _slack_inline_code_reading(text: str) -> str:
    """Collapse single-backtick code while reading the surrounding mrkdwn."""
    pieces: list[str] = []
    cursor = 0
    for code in TELEGRAM_FALLBACK_INLINE_CODE.finditer(text):
        pieces.append(_slack_non_code_reading(text[cursor : code.start()]))
        pieces.append(code.group(1))
        cursor = code.end()
    pieces.append(_slack_non_code_reading(text[cursor:]))
    return "".join(pieces)


def slack_mrkdwn_reading(text: str) -> str:
    """Model Slack mrkdwn without interpreting markup inside code spans."""
    pieces: list[str] = []
    cursor = 0
    while True:
        fence_start = text.find("```", cursor)
        if fence_start < 0:
            break
        fence_end = text.find("```", fence_start + 3)
        if fence_end < 0:
            break
        pieces.append(_slack_inline_code_reading(text[cursor:fence_start]))
        pieces.append(text[fence_start + 3 : fence_end])
        cursor = fence_end + 3
    pieces.append(_slack_inline_code_reading(text[cursor:]))
    return "".join(pieces)


def settled_display_form(
    text: str,
    redactor: Callable[[str], str],
    *,
    reading: Callable[[str], str] | None = None,
) -> str:
    """Render and redact *text* until both forms stop changing.

    :func:`canonicalize_display` is not idempotent. Its link pass runs first, and
    a pass after it can close a link the link pass went past: ``[AKIA]*(u)`` is
    text to the link pass, then the emphasis pass drops the ``*`` and leaves
    ``[AKIA](u)``, a link on every renderer. A collapsed inner link can likewise
    leave an outer one whole. So the canonical form the scan cleared is not what
    the reader sees once a renderer renders it, and the text emitted has to be
    one whose literal and rendered forms were scanned. *reading* lets a sink
    supply its exact rendering semantics.

    Each pass scans the WHOLE text, tags included, because that is what a reader
    is shown; what it emits is the rendering outside every redactor-owned tag
    (:func:`redacted_rendering`), so a tag an earlier pass wrote keeps its bytes
    and its count. Text is emitted only once its literal form and its whole-text
    rendering are both left unchanged by the redactor.

    The passes are bounded, because every one is a full scan of outbound text.
    Text still unsettled at the bound is stripped of every markup character
    outside a redactor-owned tag, read, and redacted once more.
    """
    render = canonicalize_display if reading is None else reading
    for _ in range(DISPLAY_SETTLING_PASSES):
        settled = redacted_rendering(text, render, redactor)
        if settled == text:
            return text
        text = settled
    return redacted_without_markup(text, redactor, reading=reading)


def redacted_rendering(
    text: str, rendering: Callable[[str], str], redactor: Callable[[str], str]
) -> str:
    """The redacted *rendering* of *text*, with every redactor-owned tag kept.

    Every path that emits a rendering, or renders again what it will emit, goes
    through here. A tag is an atom: no rendering may change its bytes, and none
    may pair its brackets with the text after it, so *rendering* is applied
    outside every tag (:func:`outside_redaction_tags`). ``[REDACTED:
    credential](rotated)`` therefore keeps its tag and its remark, where the
    whole-text link pass would emit the bare label and delete both.

    The whole-text rendering is still what a reader is shown. Where the redactor
    leaves the tag-kept rendering alone but changes the whole-text one, a reader
    joins a key only across a tag: the tag sat inside a construct that reader
    collapses, such as the destination of ``[]([REDACTED: credential])``. That
    reading's redacted form is emitted instead, since it is what the reader sees.
    Tags that the whole-text rendering removed are appended after that form so
    every redactor warning and count remains visible.
    """
    kept = outside_redaction_tags(text, rendering)
    kept_safe = redactor(kept)
    if kept_safe != kept:
        return kept_safe
    whole = rendering(text)
    if whole == kept:
        return kept
    whole_safe = redactor(whole)
    if whole_safe == whole:
        return kept
    return " ".join((whole_safe, *_tags_the_rendering_dropped(text, whole)))


def _tags_the_rendering_dropped(text: str, rendered: str) -> list[str]:
    """The redactor-owned tags of *text* that are missing from *rendered*, in order.

    A tag written twice and kept once is dropped once.
    """
    still_rendered = Counter(match.group(0) for match in _REDACTION_TAG.finditer(rendered))
    dropped: list[str] = []
    for tag in (match.group(0) for match in _REDACTION_TAG.finditer(text)):
        if still_rendered[tag]:
            still_rendered[tag] -= 1
        else:
            dropped.append(tag)
    return dropped


def redacted_without_markup(
    text: str,
    redactor: Callable[[str], str],
    *,
    reading: Callable[[str], str] | None = None,
) -> str:
    """The form emitted past a settling bound: *text* stripped of every markup
    character, heading marker and invisible format character outside a redaction
    tag, read outside every tag, and redacted.

    With none of them left outside a tag, no reading has a delimiter, a heading
    marker or a format character to consume outside a tag. A tag's ``]`` beside a
    ``(`` is the one construct left, and a reader that collapses it as a link
    shows the tag's own text, which is the label, with the parenthesised text
    gone. So the whole-text canonical form, every further reading and the sink's
    *reading* are scanned once more, and where one of them shows a key its
    redacted form is emitted, bounded as the settling passes are. The result
    shows a key under no reading; :func:`_settled_under_every_reading` rests on
    that.
    """
    render = canonicalize_display if reading is None else reading
    stripped = _strip_markup_outside_redaction_tags(text)
    candidate = redactor(outside_redaction_tags(stripped, render))
    renderings: tuple[Callable[[str], str], ...] = (canonicalize_display, *FURTHER_READINGS)
    if reading is not None:
        renderings = (*renderings, reading)
    for _ in range(DISPLAY_SETTLING_PASSES):
        joining = _a_reading_joining_a_key(candidate, redactor, candidate, renderings)
        if joining is None:
            return candidate
        candidate = redacted_rendering(candidate, joining, redactor)
    return candidate


def outside_redaction_tags(text: str, rewrite: Callable[[str], str]) -> str:
    """*text* with *rewrite* applied to every span outside a redactor-owned tag.

    Each tag is kept byte for byte, so it still matches the tag shape on the walk
    that follows and is counted as one tag in the text delivered.
    """
    pieces: list[str] = []
    cursor = 0
    for tag in _REDACTION_TAG.finditer(text):
        pieces.append(rewrite(text[cursor : tag.start()]))
        pieces.append(tag.group(0))
        cursor = tag.end()
    pieces.append(rewrite(text[cursor:]))
    return "".join(pieces)


def _strip_markup_outside_redaction_tags(text: str) -> str:
    """Remove every display-markup character, heading marker and invisible format
    character that is not part of a redaction tag.

    The heading marker pairs with nothing, yet the plain reading's heading pass
    removes a ``#`` run that opens a line, and this form keeps the whitespace the
    pass needs after it, so the marker itself goes. Format characters go because
    the canonical form and every further reading drop them, while the redactor
    scans the form as written.
    """
    return outside_redaction_tags(
        text,
        lambda span: _strip_format_chars(DISPLAY_MARKUP.sub("", span).replace("#", "")),
    )


#: The readings scanned besides the canonical form: our Telegram fallback, a
#: depth-unbounded balanced close, a lazy first-``)`` close, and no Markdown link
#: parsing. A client that closes a destination at another ``)`` remains outside
#: these readings.
FURTHER_READINGS: tuple[Callable[[str], str], ...] = (
    _plain_reading,
    _balanced_link_reading,
    _first_close_reading,
    _link_free_reading,
)

#: The further readings that equal the canonical form on text holding no ``](``
#: (see :func:`holds_a_link_opener`).
LINK_READINGS: tuple[Callable[[str], str], ...] = (
    _balanced_link_reading,
    _first_close_reading,
    _link_free_reading,
)


def holds_a_link_opener(text: str) -> bool:
    """Can a link reading of *text* differ from its canonical form?

    Only where *text* holds a literal ``](``. Each link collapse is the identity
    without one and the canonical link grammar cannot match without one either,
    so on text with none the canonical, balanced, first-close and link-free
    readings are all ``_display_form(text, identity)`` and the canonical form
    stands for the four; the plain fallback drops what the others keep and is
    graded regardless. The guarantee is about the INPUT text: a reading of it can
    hold a ``](`` that a display pass formed (the emphasis pass turns ``]*(`` into
    ``](``), which is harmless because every caller choosing its readings by this
    predicate passes each reading to the redactor and compares, never rendering
    it again. A grade over several pieces asks this of their JOIN: a
    ``]`` closing one piece and ``(`` opening the next is an opener only there,
    and every piece is a substring of it.
    """
    return "](" in text


def _a_reading_joining_a_key(
    text: str,
    redactor: Callable[[str], str],
    scanned: str,
    renderings: Sequence[Callable[[str], str]] | None = None,
) -> Callable[[str], str] | None:
    """The first of *renderings* whose reading of *text* the redactor changes.

    Each reading is scanned whether or not the canonical form differs from *text*:
    the canonical grammar refuses a link the narrower one accepts, so a text can
    canonicalize to itself while a reading still joins a key across the link.
    *scanned* is a form of *text* the redactor has already left unchanged; a
    reading equal to it is not scanned again. The further readings are the default
    set. ``None`` when no reading shows a key.
    """
    for reading in FURTHER_READINGS if renderings is None else renderings:
        shown = reading(text)
        if shown != scanned and redactor(shown) != shown:
            return reading
    return None


def _settled_under_every_reading(text: str, redactor: Callable[[str], str]) -> str:
    """The settled form of the redacted canonical *text* that shows a key under
    no reading.

    The canonical redacted form is the preferred answer, because it drops every url
    and collapses every link, and a key a reading joined inside a url or across a
    link goes with them. It is settled first, and emitted when every reading of the
    settled form shows no key. Where the canonical grammar kept a link raw, a
    reading still joins the key across it: that reading is redacted outside every
    tag (:func:`redacted_rendering`) and settled, and the result is scanned under
    every reading again, because a text can carry one key a reading joins and a
    second key another reading joins, and answering the first reading alone hands
    the reader the second. Redaction settles the same way rendering does: each pass
    removes what a reading showed, until a pass shows nothing. The passes are
    bounded, because every one scans the text under every reading. Text still
    showing a key at the bound is stripped of every markup character outside a
    redactor-owned tag (:func:`redacted_without_markup`), which leaves no reading
    anything to join outside a tag.
    """
    candidate = settled_display_form(text, redactor)
    for _ in range(DISPLAY_SETTLING_PASSES):
        joining = _a_reading_joining_a_key(candidate, redactor, candidate)
        if joining is None:
            return candidate
        candidate = settled_display_form(redacted_rendering(candidate, joining, redactor), redactor)
    return redacted_without_markup(candidate, redactor)


def joins_to_a_credential(head: str, tail: str, redactor: Callable[[str], str]) -> bool:
    """Would a reader shown *head* and then *tail* see a key neither half holds?

    A cap that cuts text into two messages is applied to the RAW string, while the
    reader sees the CANONICAL rendering of each piece. So a credential the model
    split with markup can be severed by the cut: each piece is scrubbed on its own
    and matches nothing, and the reader's client renders the markup away and
    rejoins the halves on screen.

    This answers the question directly rather than guessing which characters could
    hide such a split. Each side is put through the same redaction the sender will
    actually apply (:func:`redact_for_display`), then reduced to what the platform
    SHOWS (:func:`canonicalize_display`), and the result of putting the two sides
    together is scanned.

    Soundness, which is the whole point: ``redact_for_display`` already emits the
    canonical form whenever canonicalising reveals something the literal form hid,
    and likewise whenever the plain-text reading (links kept, delimiters dropped)
    reveals a key the canonical form dropped along with a link's url. On those
    branches the emitted text has been rendered again until rendering it changes
    nothing, so it IS its own canonical reading and that reading scans clean; on
    the pass-through branch the text's canonical reading is the ``canonical`` the
    function already scanned clean. Either way, for any single string ``x``,
    ``canonicalize_display(redact_for_display(x)[0])`` is text the redactor
    leaves unchanged. Whatever this scan finds is therefore
    produced by putting the two sides together and by nothing else. No character
    class, no window and no
    anchor list: a search window built from a hand-written set of characters cannot
    be closed, because the next character the set does not know about is one more
    place a split can hide -- the walk that finds the window's edge stops there, the
    check runs on a span the credential's prefix was never inside, and it passes
    vacuously.

    BOTH readings a reader can produce are scanned, because neither one contains
    the other:

    * **canonicalise the join** models a COPY of both messages, and a client
      lenient about where one message ends. It is the wider reading for runs of
      delimiters, which concatenation can only extend: ``AKIA**`` beside
      ``**REST`` is a run only once the halves sit together.
    * **canonicalise each side, then join** models the screen -- two messages
      rendered separately, read one after the other. This is the wider reading
      wherever canonicalising DROPS text rather than just deleting delimiters,
      which is exactly what a link does to its target. A cut one character inside
      ``[l](https://x/AKIA`` + ``REST)`` completes the link only in the join,
      where the url then collapses to the label and the key vanishes from the
      scan -- while on screen each half is an unfinished link whose url stays
      visible, and the reader reads straight through it.

    So the join alone would pass a cut through a credential in a url, and the
    per-side reading alone would miss a credential split by markup at the
    boundary. Either reading finding something is enough to refuse the cut, and
    ``test_display_split_safety.py`` pins one shape per reading.

    The further readings :func:`redact_for_display` scans
    (:data:`FURTHER_READINGS`) are taken both ways as well, because a pair has to
    be graded under every reading a single message is: Telegram's fallback and its
    HTML seal both drop a heading marker, so a field name closing one message and
    ``#   : <value>`` opening the next read as the assignment on screen, while the
    literal join and the canonical form both keep the ``#`` between them and scan
    clean. Each reading is scanned once, and the scan stops at the first that
    shows a key.
    """
    head_safe = redact_for_display(head, redactor)[0]
    tail_safe = redact_for_display(tail, redactor)[0]
    renderings: tuple[Callable[[str], str], ...] = (canonicalize_display, *FURTHER_READINGS)
    readings = (
        reading
        for render in renderings
        for reading in (render(head_safe + tail_safe), render(head_safe) + render(tail_safe))
    )
    return any(redactor(reading) != reading for reading in readings)


def severs_a_credential(
    pieces: Sequence[str],
    redactor: Callable[[str], str],
    present: Callable[[str], str] | None = None,
) -> bool:
    """Would a reader shown *pieces* in order see a key that none of them holds?

    The n-piece case of :func:`joins_to_a_credential`. A renderer whose cap forces
    one buffer into several messages redacts each message ALONE, so a credential the
    split severed matches nothing in any single message -- and the reader's client
    renders the markup away and reads the pieces one under the other, in order, as
    one text.

    Two readings, because neither subsumes the other:

    * **the whole sequence**, every piece redacted alone and then put together. This
      is what catches a key whose MARKUP spans an entire piece rather than whose
      characters do: cut ``AKIA[KEYTAIL](http://host/<long>)`` into three and no
      neighbouring pair canonicalises to anything (the link needs its closing
      bracket, which is in the third piece), while the full join collapses the url
      to the label and puts the label against ``AKIA``. Piece length is no defence
      here -- canonicalising DROPS a link's target, so a piece of any size can
      vanish entirely.
    * **each boundary read BOTH ways round** -- everything after it, and everything
      before it. This is the reading that survives a pattern anchored at its edges:
      the reader sees a message break where the whole-sequence join sees the next
      character, so a key completed at the end of one piece must be caught even when
      later text would spoil the match. Both ways, because the patterns anchor on
      BOTH sides: reading only forwards misses a key finished at a boundary whose
      NEXT piece opens on a character the trailing class rejects, which every
      forward reading carries.

    Every piece is put through the same redaction the sender will apply and then
    reduced to what the platform SHOWS, exactly as :func:`joins_to_a_credential`
    does for a single cut. Soundness rests on the same property:
    ``redact_for_display`` is already a fixed point for one string, so whatever
    either reading finds is produced by putting pieces together and by nothing else.
    One piece therefore answers False: there is no boundary.

    *present* maps a piece to the form the platform will actually DELIVER, and when
    it is given BOTH forms are graded -- a sever in either answers True. Both,
    because neither form dominates the other and a caller that picked one would miss
    the half the other sees:

    * trimming REVEALS a join. ``AKIAIOSF\\n\\n`` and ``   ODNN7EXAMPLE`` are kept
      apart by whitespace no credential pattern tolerates, and sit flush on screen
      once the sink strips it.
    * trimming CONCEALS one. ``-----BEGIN `` and ``RSA PRIVATE KEY-----`` need that
      trailing space to match, so the trimmed pair reads clean while the delivered
      pair -- on a sink that does not trim, or trims less than modelled -- is an
      intact PEM anchor.

    So "model more trimming and the grade only gets stricter" is FALSE, and grading
    the delivered form alone is not the safe direction. Grading both is.

    True means the split is unusable and the caller must cut somewhere else (see
    :func:`safe_split_offset`) or deliver nothing at all.
    """
    forms = [list(pieces)]
    if present is not None:
        forms.append([present(piece) for piece in pieces])
    return any(_sequence_severs(form, redactor) for form in forms)


def _sequence_severs(pieces: Sequence[str], redactor: Callable[[str], str]) -> bool:
    """One reading pass over *pieces* as given. See :func:`severs_a_credential`."""
    safe = [redact_for_display(piece, redactor)[0] for piece in pieces]
    return any(redactor(reading) != reading for reading in _sequence_readings(safe))


def _sequence_readings(safe: Sequence[str]) -> Iterator[str]:
    """Every reading of *safe* a reader can assemble. Lazy, so a sever exits early."""
    joined = "".join(safe)
    # Without a ``](`` opener, canonical, balanced, first-close and link-free
    # rendering are identical for every prefix, suffix and interior window.
    renderings = (
        (canonicalize_display, *FURTHER_READINGS)
        if holds_a_link_opener(joined)
        else (
            canonicalize_display,
            *(reading for reading in FURTHER_READINGS if reading not in LINK_READINGS),
        )
    )
    alone_by_render = tuple(tuple(render(piece) for piece in safe) for render in renderings)
    # On a window with no ``](`` opener, canonical, balanced, first-close and
    # link-free rendering are all ``_display_form(window, identity)``: each link
    # collapse is the identity without the literal ``](``, and ``_MD_LINK`` cannot
    # match without it either. The window is rendered once for the four and the
    # result reused, matched by which function a slot grades, never by position.
    # Every other rendering (the plain fallback) is computed on its own.
    link_readings = (canonicalize_display, *LINK_READINGS)

    def renders_of(window: str) -> tuple[str, ...]:
        """Render *window* under every rendering, in ``renderings`` order."""
        if holds_a_link_opener(window):
            return tuple(render(window) for render in renderings)
        shared = canonicalize_display(window)
        return tuple(shared if render in link_readings else render(window) for render in renderings)

    def unique_slot(readings: tuple[str, ...]) -> Iterator[str]:
        """Yield distinct readings for one slot while holding at most one per render."""
        for index, reading in enumerate(readings):
            if reading not in readings[:index]:
                yield reading

    yield from unique_slot(renders_of(joined))
    yield from unique_slot(tuple("".join(alone) for alone in alone_by_render))
    # Each boundary read BOTH ways round, because the patterns anchor at BOTH ends
    # and a reading that runs past either one is spoiled there:
    #
    # * everything AFTER the boundary -- a key whose first character opens a
    #   message, where the message before it ends in a character the leading
    #   ``(?<![A-Za-z0-9_.-])`` class rejects.
    # * everything BEFORE the boundary -- a key COMPLETED at the end of a message,
    #   where the next message opens with a character the trailing
    #   ``(?![A-Za-z0-9_-])`` class rejects. EVERY reading that runs to the end of
    #   the sequence carries that character and reports clean, so without this half
    #   a key finished across messages 1 and 2, with message 3 starting on a
    #   letter, is one key on screen and invisible here.
    #
    # Both halves in both forms -- joined, and each message reduced under each
    # reading alone -- for the reason the whole-sequence pair is read twice.
    #
    # One reading per boundary, each over a prefix or a suffix. That is a linear
    # NUMBER of readings; the bytes they copy are not linear, because a prefix and
    # a suffix are rebuilt at every boundary, so the pass is quadratic in total
    # bytes on a sequence with many pieces. The bound that matters for an author-
    # controlled input is the piece count, and the interior windows below are what
    # keep the reading count linear in it.
    for index in range(len(safe) - 1):
        prefix = "".join(safe[: index + 1])
        rest = "".join(safe[index + 1 :])
        yield from unique_slot(renders_of(prefix))
        # The suffix renders are graded here and again in the lump slot below.
        suffix_renders = renders_of(rest)
        yield from unique_slot(suffix_renders)
        yield from unique_slot(tuple("".join(alone[: index + 1]) for alone in alone_by_render))
        yield from unique_slot(tuple("".join(alone[index + 1 :]) for alone in alone_by_render))
        # The head against the whole remainder as ONE lump, which reducing each
        # message alone does not subsume: a lump collapses markup spanning two
        # messages inside it that each message alone leaves intact.
        yield from unique_slot(
            tuple(alone[index] + shown for shown, alone in zip(suffix_renders, alone_by_render))
        )
    # INTERIOR runs, bounded. A credential can also sit with a spoiling message on
    # EACH side of it, and no reading above reaches that: every prefix, suffix and
    # whole reading carries at least one of the two frames, where the anchor classes
    # reject the match. A rotation of model text makes both frames ordinary rather
    # than contrived -- the splitter rstrips each chunk, so an alphanumeric edge is
    # the common case, not a crafted one.
    #
    # Reading EVERY contiguous run would catch a credential spread over any number
    # of pieces, and is quadratic in the piece count -- which an author controls, a
    # degenerate rotation splitting into thousands of pieces (2451 for a
    # 5000-backtick run at a 100 budget). So the run LENGTH is capped instead: a
    # window of at most ``_INTERIOR_RUN_PIECES`` messages, slid across the interior,
    # is a linear number of readings over bounded-size strings. A credential spread
    # thinner than the cap, with a spoiling frame on each side, is the residual and
    # is tracked rather than read here.
    for start in range(1, len(safe) - 1):
        for stop in range(
            start + 2,
            min(start + _INTERIOR_RUN_PIECES, len(safe) - 1) + 1,
        ):
            interior = "".join(safe[start:stop])
            yield from unique_slot(renders_of(interior))
            yield from unique_slot(tuple("".join(alone[start:stop]) for alone in alone_by_render))


def safe_split_offset(
    text: str,
    limit: int,
    redactor: Callable[[str], str],
    present: Callable[[str], str] | None = None,
) -> int:
    """The largest SAMPLED offset at or below *limit* that severs no credential.

    *present* maps a side to the form the platform will actually DELIVER, and an
    offset is accepted only when NEITHER the raw pair nor the presented pair severs.
    Both, for the reason :func:`severs_a_credential` sets out: trimming reveals a
    join where whitespace kept the edges apart, and conceals one where the pattern
    needs that whitespace to match, so neither form dominates. It has to be the
    caller's own, because only the caller knows what its sink strips. Default is
    identity, for a caller that delivers its text verbatim.

    Not the largest safe offset: the candidates are sampled, so a safe offset
    between two samples is passed over. Those characters are not lost, only
    deferred to the next delivery.

    Used by a renderer whose message cap forces *text* into two deliveries: cut
    here and :func:`joins_to_a_credential` is false of both forms, so the reader
    cannot rejoin a key across the boundary.

    Candidates step back EXPONENTIALLY (``limit``, then 1, 2, 4, 8 ... characters
    before it), for a cost bound: the nearest safe boundary is not needed, only a
    safe one, and stepping past it merely defers a few more characters to the next
    delivery. A linear walk would be O(*limit*) redaction passes over
    attacker-influenced text on every frame; this is O(log *limit*), and the common
    case -- prose, where any cut is safe -- costs one pass, or none at all when
    *text* already fits.

    ``0`` means every SAMPLED candidate was unsafe -- one matched region covers all
    of them. A safe offset between two samples may still exist; the search does not
    look for it, because the answer it needs is only "is there a safe cut I can take
    now". Callers treat ``0`` as "deliver nothing yet", which is always available to
    them: text withheld now is text the next delivery carries.
    """
    if limit <= 0:
        return 0
    if limit >= len(text):
        # Nothing is severed, so there is no boundary to check.
        return len(text)
    offset, step = limit, 0
    while offset > 0:
        if not severs_a_credential([text[:offset], text[offset:]], redactor, present):
            return offset
        step = 1 if step == 0 else step * 2
        offset = limit - step
    return 0


def redact_for_display(text: str, redactor: Callable[[str], str]) -> tuple[str, bool]:
    """Redact *text* against what the platform will DISPLAY, not just the bytes.

    Two normalisations, for the same underlying reason: a transformation applied
    *after* the scan can reassemble a credential the scanner saw as broken.

    1. **ANSI escapes** -- stripped outright, because they are display noise with
       no meaning to preserve.
    2. **Link markup and emphasis/code delimiters** -- these DO carry meaning, so
       they cannot simply be deleted. Instead the canonical (display) form is
       scanned as well. Neither ``AKIA**<rest>**`` nor ``[AKIA](https://x)<rest>``
       matches a credential pattern as written, yet the platform renders the
       markup away and shows the reader an intact key.

    When the canonical form reveals a secret that the literal form hid, the
    canonical text is emitted -- so the message loses that markup. That is
    deliberate and one-directional: formatting is worth less than a credential, and
    the downgrade only happens on a message that actually contains one.

    The canonical form drops every url, because a rendering platform shows only a
    link's label. Telegram's plain-text fallback can instead print that url after
    the Markdown screen has run, removing fences, inline code, ``**`` and ``__``
    and then flattening a link to ``label (url)``, in that relative order.
    :func:`_plain_reading` applies the same pattern objects and replacements
    (:data:`TELEGRAM_FALLBACK_PASSES`, which the renderer imports from here), so
    the screen and the sink cannot drift. The order matters as much as the set:
    the fallback's link pass runs after its delimiter passes, so it links
    ``AKIA[IOSF...]**(u)**`` once the ``**`` is gone, where the screen's own link
    pass, running first, sees text. Telegram's heading rewrite is shared because
    removing its marker can expose a named credential assignment; its bullet
    rewrite keeps a visible bullet between the runs and remains renderer-local.
    When the fallback reading reveals a key, the canonical redacted form is
    emitted, taking the key away with the link's target or, for a link the
    delimiter passes close, with the link itself once the settling passes below
    have collapsed it.

    The screen also scans a depth-unbounded balanced close, a lazy first-``)``
    close, and a link-free reading that leaves every url visible. A key only one
    of these readings reveals is answered with the canonical redacted form.

    A text that changes on any route is emitted only once it shows a key under no
    reading (:func:`_settled_under_every_reading`). The canonical redacted form is
    preferred, because it carries no url; where a reading still joins a key across
    a link the canonical grammar kept raw, that reading is redacted and settled,
    and the result is scanned under every reading again. One text can carry a key
    one reading joins and a second key another reading joins, so answering the
    first reading alone would hand the reader the second. The passes are bounded;
    text still showing a key at the bound is stripped of every markup character
    outside a redactor-owned tag, which leaves no reading anything to join
    outside a tag.

    The canonical form emitted on any route is rendered again until rendering
    it changes nothing (:func:`settled_display_form`, bounded, then stripped of
    markup), because canonicalising can close a link that the scanned reading
    still showed as text: the emphasis pass turns ``[AKIA]*(u)`` into ``[AKIA](u)``
    after the link pass has gone by, so the scan cleared a text whose own
    rendering joins the key.

    Every form scanned is the whole text, tags included, because that is what a
    reader is shown. Every form EMITTED is rendered outside the tags the redactor
    wrote (:func:`redacted_rendering`): a tag is an atom, and a rendering that
    read ``[REDACTED: credential](rotated)`` as a link would emit the bare label,
    delete the remark and leave the delivered text with no tag to count.

    Returns:
        ``(safe_text, redacted)``. ``redacted`` is True when the redactor changed
        anything, by any route -- callers that already published the text rely
        on it to go back and replace what is visible.
    """
    stripped = strip_ansi(text or "")
    safe = redactor(stripped)
    changed = safe != stripped

    literal = _strip_format_chars(safe)
    if literal != safe:
        literal_safe = redactor(literal)
        if literal_safe != literal:
            safe, changed = literal_safe, True
    canonical = canonicalize_display(safe)
    canonical_shows_a_key = canonical != safe and redactor(canonical) != canonical
    if not canonical_shows_a_key and _a_reading_joining_a_key(safe, redactor, canonical) is None:
        return safe, changed
    # The markup was hiding a credential from the scan. Emit the canonical,
    # redacted form: losing formatting beats leaking the key. The literal scan
    # may already have written a tag into ``safe``, so the form emitted is
    # rendered outside every tag; the whole-text form scanned is the reader's.
    emitted = redacted_rendering(safe, canonicalize_display, redactor)
    return _settled_under_every_reading(emitted, redactor), True

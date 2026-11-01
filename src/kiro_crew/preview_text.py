"""Markdown → plain-text stripping for one-line UI previews.

The sidebar/session-list preview (`last_message` in slot ``to_dict()`` and
``HistoryLog.last_message_preview``) is a single truncated line rendered as
plain text — raw markdown markers (``**bold**``, ``` ```diff `` fences, link
syntax) read as noise there. This helper strips markdown down to readable
plain text, Slack/Telegram-preview style, WITHOUT rendering it.

Deliberately gentler than ``voice_reply.strip_markdown`` (which optimizes for
speech): emoji are kept (though ZWJ-joined sequences decompose into their
constituent emoji, since Unicode format characters are dropped — see
``drop_format_chars``), inline code keeps its literal text, and single
underscores are left alone so ``snake_case`` identifiers survive intact.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable

from kiro_crew.constants import OPTIONS_RE_LINE, md_link_destination, strip_control_comments

# Fenced code blocks — ```lang ... ``` or a line-anchored ~~~lang ... ~~~ (or
# unterminated, running to the end of the message). Replaced with a short
# placeholder; the code body would dominate a 80–120 char preview otherwise.
# One alternation, so whichever fence opens first wins and a ``` inside a ~~~
# block (or the reverse) stays part of that block.
_FENCE_RE = re.compile(
    r"```([^\n`]*)\n?[\s\S]*?(?:```|\Z)"
    # A ~~~ fence closes only on a run at least as long as its opener.
    r"|^[^\S\n]{0,3}(?P<tildes>~{3,})([^\n]*)\n?[\s\S]*?"
    r"(?:^[^\S\n]{0,3}(?P=tildes)~*[^\S\n]*$|\Z)",
    re.MULTILINE,
)
# <mcwidget> bodies render as an iframe elsewhere; raw HTML is noise here.
_MCWIDGET_RE = re.compile(r"<mcwidget\b[^>]*>[\s\S]*?(?:</mcwidget>|\Z)", re.IGNORECASE)
# Control-tag comment stripping lives in ``constants.strip_control_comments``
# (grammar + recognizer split documented on ``constants._TRAILING_CONTROL_LINES_RE``);
# this module deliberately has no local spelling to drift.
# Inline code spans are swapped for ``\x00<n>\x00`` tokens while the link,
# header and emphasis passes run, then put back, so ``__init__``, ``**kwargs``
# and ``~~x~~`` inside backticks keep their markers. NUL renders as nothing, so
# any NUL in the input is dropped first and cannot forge a token.
_BACKTICK_RUN_RE = re.compile(r"`+")
_CODE_TOKEN_RE = re.compile(r"\x00(\d+)\x00")
#: A link destination keeps a balanced ``(...)`` pair, as the channel renderers
#: read it; see :func:`kiro_crew.constants.md_link_destination`. A label stops at
#: the next ``[``, so a run of ``[`` cannot make every opener rescan the text.
_LINK_DESTINATION_UNIT = md_link_destination("[^()]")
_IMAGE_RE = re.compile(rf"!\[([^\[\]]*)\]\({_LINK_DESTINATION_UNIT}*\)")
_LINK_RE = re.compile(rf"\[([^\[\]]+)\]\({_LINK_DESTINATION_UNIT}*\)")
# Trailing quick-reply block — rendered as buttons, not text. Reuse the canonical
# ReDoS-hardened, line-anchored parser (constants.OPTIONS_RE_LINE) so this strip
# can't drift from the dashboard/Slack copies and handles `]` inside a label.
_OPTIONS_RE = OPTIONS_RE_LINE
_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
# Line-anchored markers: leading whitespace is HORIZONTAL only (``[^\S\n]``,
# i.e. ``\s`` minus newline). A MULTILINE ``^\s*`` lets the quantifier run
# PAST the current line across a whole run of blank lines, so every ``^``
# anchor in that run rescans the run's remainder — polynomial backtracking
# (CWE-1333, py/polynomial-redos) that wedged the gateway event loop for
# 25s+ on whitespace-heavy previews and crash-looped the watchdog. Matching
# within one line keeps the scan linear and the semantics identical: ``^``
# already matches at the marker's own line, and the trailing whitespace
# collapse eats any newlines the old pattern happened to consume.
_BLOCKQUOTE_RE = re.compile(r"^[^\S\n]*>\s?", re.MULTILINE)
_BULLET_RE = re.compile(r"^[^\S\n]*(?:[-*+]|\d+\.)\s+", re.MULTILINE)
_HR_RE = re.compile(r"^[^\S\n]*([-*_])\1{2,}[^\S\n]*$", re.MULTILINE)
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"\*([^*\n]+)\*")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)


def _fence_placeholder(m: re.Match) -> str:
    lang = (m.group(1) or m.group(3) or "").strip().lower()
    return " (diff) " if lang == "diff" else " (code) "


def _protect_inline_code(text: str, spans: list[str]) -> str:
    """Swap each inline code span in *text* for a ``\\x00<n>\\x00`` token.

    A span opens on a run of N backticks and closes on the next run of exactly
    N backticks on the same line, as CommonMark reads it; the code goes into
    *spans* without its delimiters (one padding space trimmed from each side
    when both are there). A run with no closer stays literal text. One pass
    over the backtick runs, so the cost stays linear in the input.
    """
    runs: list[tuple[int, int, int]] = []
    line = 0
    last = 0
    for m in _BACKTICK_RUN_RE.finditer(text):
        line += text.count("\n", last, m.start())
        last = m.start()
        runs.append((m.start(), m.end(), line))
    # closer[i] = index of the next run with the same length on the same line.
    closer: list[int | None] = [None] * len(runs)
    latest: dict[tuple[int, int], int] = {}
    for i in range(len(runs) - 1, -1, -1):
        start, end, line = runs[i]
        key = (line, end - start)
        closer[i] = latest.get(key)
        latest[key] = i
    out: list[str] = []
    pos = 0
    i = 0
    while i < len(runs):
        j = closer[i]
        if j is None:
            i += 1
            continue
        code = text[runs[i][1] : runs[j][0]]
        if len(code) > 2 and code[0] == " " and code[-1] == " " and code.strip(" "):
            code = code[1:-1]
        out.append(text[pos : runs[i][0]])
        out.append(f"\x00{len(spans)}\x00")
        spans.append(code)
        pos = runs[j][1]
        i = j + 1
    out.append(text[pos:])
    return "".join(out)


def drop_format_chars(text: str) -> str:
    """Remove Unicode format characters (category Cf) from *text*.

    Cf covers the zero-width space/joiners, the word joiner, BOM, bidi
    controls and the soft hyphen: characters that render as nothing yet are
    truthy in string guards, and that ``str.split()`` does NOT treat as
    whitespace. Quiet monitor-loop cycles post a bare U+200B as their
    say-nothing assistant reply, so without this step a message of pure
    format characters survives the whitespace collapse as a truthy-but-
    invisible preview — defeating every empty-preview fallback downstream
    (the sidebar subtitle and archived-session previews both skip a row only
    when its preview is empty). Dropping the whole Cf category rather than an
    enumerated zero-width set closes the class, not one codepoint; the cost
    is that ZWJ-joined emoji sequences decompose into their constituent emoji
    in a one-line preview.

    This is the ONE implementation of the Cf drop:
    ``messaging.display_safety._strip_format_chars`` delegates here, and its
    docstring carries the security half of the contract — the drop can
    REASSEMBLE a credential that format characters had split, so callers must
    strip BEFORE running pattern-based redaction, never after. The ASCII fast
    path is sound, not an approximation: the ASCII range holds no Cf code
    point (the C0 controls are Cc), so ordinary traffic never pays for the
    per-character walk.

    Each DISTINCT character is categorized once and ``str.translate`` removes
    the Cf ones: a long message repeats a few dozen code points, so this drops
    exactly the characters a per-character walk would, in C.
    """
    if text.isascii():
        return text
    drop = {ord(ch): None for ch in set(text) if unicodedata.category(ch) == "Cf"}
    return text.translate(drop) if drop else text


#: The one length a roster preview is cut to. Both spellings of a member's
#: ``last_message`` (the cold transcript read and the live ``member/message``
#: event) go through :func:`speech_preview`, so this is the only place the cap lives.
PREVIEW_MAX_CHARS = 120


def speech_preview(
    text: str,
    sanitize: Callable[[str], str] | None = None,
    max_chars: int = PREVIEW_MAX_CHARS,
) -> str:
    """The roster's one-line quote of a spoken message, built the ONE way.

    Markdown is stripped first (:func:`strip_markdown_preview`), *sanitize* --
    the caller's redaction chain -- runs on the stripped text, and only then is
    the result cut to *max_chars* with a trailing ellipsis. Sanitization precedes
    truncation so a boundary cannot hide a credential fragment from a
    pattern-based redactor. Returns ``""`` when nothing visible remains.

    Two writers spell a member's ``last_message``: the cold transcript read
    (``last_speech_info``) and the live ``member/message``
    event the dashboard appends as the row lands. They MUST agree byte for byte,
    because the roster read corrects the folded preview whenever the two differ
    and would otherwise append a correction after every formatted message. Both
    call this and nothing else.
    """
    preview = strip_markdown_preview(text)
    if not preview:
        return ""
    if sanitize is not None:
        preview = sanitize(preview)
    if len(preview) > max_chars:
        preview = preview[:max_chars].rstrip() + "…"
    return preview


def strip_markdown_preview(text: str) -> str:
    """Best-effort plain text for a one-line preview of *text*.

    Strips markdown syntax (fences → ``(code)``/``(diff)`` placeholders,
    emphasis markers, link/image syntax, headers, quote/bullet markers) and
    collapses all whitespace to single spaces. Unicode format characters
    (category Cf — the zero-width space and friends) are dropped first, so a
    message that renders as nothing yields an empty preview and downstream
    empty-preview fallbacks fire. Truncation is the caller's job — this only
    cleans.
    """
    t = drop_format_chars(text).replace("\x00", "")
    t = _FENCE_RE.sub(_fence_placeholder, t)
    t = _MCWIDGET_RE.sub(" (widget) ", t)
    # Only RECOGNIZED control tags (keep-visible, heartbeat deliver
    # routing, plan_task_id anchors) — never all comments, and never inside
    # inline code: a tag an assistant quotes in inline code renders literally
    # and is visible content. Shared implementation; see constants.py.
    t = strip_control_comments(t)
    # The quick-reply line goes before code is protected: a backtick-wrapped
    # ``[OPTIONS: ...]`` line still renders as buttons, not as text.
    t = _OPTIONS_RE.sub("", t)
    spans: list[str] = []
    t = _protect_inline_code(t, spans)
    t = _IMAGE_RE.sub(lambda m: m.group(1) or "(image)", t)
    t = _LINK_RE.sub(r"\1", t)
    # Line-anchored markers must go before whitespace collapse.
    t = _HR_RE.sub("", t)
    t = _HEADER_RE.sub("", t)
    t = _BLOCKQUOTE_RE.sub("", t)
    t = _BULLET_RE.sub("", t)
    t = _BOLD_RE.sub(r"\2", t)
    t = _ITALIC_STAR_RE.sub(r"\1", t)
    t = _STRIKE_RE.sub(r"\1", t)
    t = _CODE_TOKEN_RE.sub(lambda m: spans[int(m.group(1))], t)
    return " ".join(t.split())

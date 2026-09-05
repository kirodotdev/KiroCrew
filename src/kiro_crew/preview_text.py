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

from kiro_crew.constants import OPTIONS_RE_LINE, strip_action_markers, strip_control_comments

# Fenced code blocks — ```lang ... ``` (or unterminated, running to the end
# of the message). Replaced with a short placeholder; the code body would
# dominate a 80–120 char preview otherwise.
_FENCE_RE = re.compile(r"```([^\n`]*)\n?[\s\S]*?(?:```|\Z)")
# <mcwidget> bodies render as an iframe elsewhere; raw HTML is noise here.
_MCWIDGET_RE = re.compile(r"<mcwidget\b[^>]*>[\s\S]*?(?:</mcwidget>|\Z)", re.IGNORECASE)
# Control-tag comment stripping lives in ``constants.strip_control_comments``
# (grammar + recognizer split documented on ``constants._TRAILING_CONTROL_LINES_RE``);
# this module deliberately has no local spelling to drift.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Trailing quick-reply block — rendered as buttons, not text. Reuse the canonical
# ReDoS-hardened, line-anchored parsers (constants.OPTIONS_RE_LINE and, for
# actions, the ``strip_action_markers`` helper that wraps its sibling) so this
# strip can't drift from the dashboard/Slack copies and handles `]` inside a
# label. Both heads are stripped and neither leaves a trace: a preview is one line
# of plain text, so a marker that survived here would be the most-seen leak in the
# product — it sits in the session list of every idle session, not just the open
# one. Actions go through the helper rather than the raw pattern because a span
# nested in an UNCLOSED marker is not a marker: excising it would drop the
# malformed text that is the reader's only cue a marker was meant.
_OPTIONS_RE = OPTIONS_RE_LINE
_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+", re.MULTILINE)
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$", re.MULTILINE)
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"\*([^*\n]+)\*")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)


def _fence_placeholder(m: re.Match) -> str:
    lang = (m.group(1) or "").strip().lower()
    return " (diff) " if lang == "diff" else " (code) "


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
    """
    if text.isascii():
        return text
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


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
    t = drop_format_chars(text)
    t = _FENCE_RE.sub(_fence_placeholder, t)
    t = _MCWIDGET_RE.sub(" (widget) ", t)
    # Only RECOGNIZED control tags (keep-visible #7948, heartbeat deliver
    # routing, plan_task_id anchors) — never all comments, and never inside
    # inline code: a tag an assistant quotes in inline code renders literally
    # and is visible content. Shared implementation; see constants.py.
    t = strip_control_comments(t)
    t = _INLINE_CODE_RE.sub(r"\1", t)
    t = _IMAGE_RE.sub(lambda m: m.group(1) or "(image)", t)
    t = _LINK_RE.sub(r"\1", t)
    t = _OPTIONS_RE.sub("", t)
    t = strip_action_markers(t)
    # Line-anchored markers must go before whitespace collapse.
    t = _HR_RE.sub("", t)
    t = _HEADER_RE.sub("", t)
    t = _BLOCKQUOTE_RE.sub("", t)
    t = _BULLET_RE.sub("", t)
    t = _BOLD_RE.sub(r"\2", t)
    t = _ITALIC_STAR_RE.sub(r"\1", t)
    t = _STRIKE_RE.sub(r"\1", t)
    return " ".join(t.split())
